import logging
from collections import defaultdict
from copy import deepcopy
from typing import Optional, Sequence, Tuple

import gymnasium as gym
import numpy as np
import torch
from diffusers.optimization import get_scheduler
from torch import nn
from tqdm import trange
from typing_extensions import override

from robobase import utils
from robobase.method.utils import (
    TimeConsistentRandomShiftsAug,
    extract_from_batch,
    extract_many_from_batch,
    extract_many_from_spec,
    stack_tensor_dictionary,
)
from robobase.models import RoboBaseModule
from robobase.models.encoder import EncoderModule
from robobase.models.fully_connected import FullyConnectedModule
from robobase.models.fusion import FusionModule
from robobase.replay_buffer.replay_buffer import ReplayBuffer
from robobase.reward_method.core import RewardMethod


class InvalidSequenceError(Exception):
    def __init__(self, message):
        super().__init__(message)


class MarkovianRewardModel(nn.Module):
    def __init__(
        self,
        reward_model: FullyConnectedModule,
        num_reward_models: int = 1,
        apply_final_layer_tanh: bool = False,
    ):
        super().__init__()
        self.rs = nn.ModuleList(
            [deepcopy(reward_model) for _ in range(num_reward_models)]
        )
        self.apply(utils.weight_init)
        self.apply_final_layer_tanh = apply_final_layer_tanh
        self.label_margin = 0.0
        self.label_target = 1.0 - 2 * self.label_margin

    def forward(self, low_dim_obs, fused_view_feats, action, time_obs, member=0):
        net_ins = {"action": action.view(action.shape[0], -1)}
        if low_dim_obs is not None:
            net_ins["low_dim_obs"] = low_dim_obs
        if fused_view_feats is not None:
            net_ins["fused_view_feats"] = fused_view_feats
        if time_obs is not None:
            net_ins["time_obs"] = time_obs

        reward_out = self.rs[member](net_ins)
        if self.apply_final_layer_tanh:
            reward_out = torch.tanh(reward_out)
        return reward_out

    def reset(self, env_index: int):
        for r in self.rs:
            r.reset(env_index)

    def set_eval_env_running(self, value: bool):
        for r in self.rs:
            r.eval_env_running = value

    def calculate_loss(
        self,
        logits: Tuple[torch.Tensor, torch.Tensor],
        labels: torch.Tensor,
    ) -> Optional[Tuple[torch.Tensor, dict]]:
        """
        Calculate the loss for the Markovian Reward Model.

        Args:
            logits (Tuple[torch.Tensor, torch.Tensor]): Tuple of two tensors, each of shape (bs, seq).
            labels (torch.Tensor): Tensor containing ground truth preference labels.

        Returns:
            Optional[Tuple[torch.Tensor, dict]]:
                    Tuple containing the loss tensor and a dictionary of loss
                    components.
        """
        logits = torch.stack(logits, dim=-1)

        loss_dict = dict(loss=0.0)
        reward_loss = 0.0
        for idx, (logit, label) in enumerate(zip(logits.unbind(1), labels.unbind(1))):
            # reward_loss = F.cross_entropy(logit, label)
            uniform_index = label == -1
            if uniform_index.ndim > 1:
                uniform_index = uniform_index.squeeze(-1)
            label[uniform_index] = 0
            target_onehot = torch.zeros_like(logit).scatter(
                1, label.unsqueeze(-1), self.label_target
            )
            target_onehot += self.label_margin
            if sum(uniform_index) > 0:
                target_onehot[uniform_index, :] = 0.5
            reward_loss += utils.softXEnt_loss(logit, target_onehot)
            loss_dict[f"pref_acc_label_{idx}"] = utils.pref_accuracy(logit, label)
            loss_dict[f"pref_loss_{idx}"] = reward_loss
            loss_dict["loss"] += reward_loss

        return loss_dict


class MarkovianReward(RewardMethod):
    def __init__(
        self,
        lr: float,
        adaptive_lr: bool,
        num_train_steps: int,
        encoder_model: Optional[EncoderModule] = None,
        view_fusion_model: Optional[FusionModule] = None,
        reward_model: Optional[RoboBaseModule] = None,
        lr_backbone: float = 1e-5,
        weight_decay: float = 1e-4,
        use_lang_cond: bool = False,
        num_labels: int = 1,
        num_reward_models: int = 1,
        seq_len: int = 50,
        compute_batch_size: int = 32,
        use_augmentation: bool = False,
        reward_space: gym.spaces.Dict = None,
        apply_final_layer_tanh: bool = False,
        data_aug_ratio: float = 0.0,
        *args,
        **kwargs,
    ):
        """
        Markovian Reward Model Agent.

        Args:
            lr (float): Learning rate for the reward model.
            lr_backbone (float): Learning rate for the backbone.
            weight_decay (float): Weight decay for optimization.
        """
        super().__init__(*args, **kwargs)

        self.lr = lr
        self.lr_backbone = lr_backbone
        self.weight_decay = weight_decay
        self.reward_space = gym.spaces.Dict(sorted(reward_space.items()))

        self.adaptive_lr = adaptive_lr
        self.num_train_steps = num_train_steps
        self.num_labels = num_labels
        self.num_reward_models = num_reward_models
        self.seq_len = seq_len
        self.apply_final_layer_tanh = apply_final_layer_tanh
        self.compute_batch_size = compute_batch_size

        self.encoder_model = encoder_model
        self.view_fusion_model = view_fusion_model
        self.reward_model = reward_model
        self.rgb_spaces = extract_many_from_spec(
            self.observation_space, r"rgb.*", missing_ok=True
        )
        self.aug = (
            TimeConsistentRandomShiftsAug(pad=4) if use_augmentation else lambda x: x
        )
        self.data_aug_ratio = data_aug_ratio

        # T should be same across all obs
        self.use_pixels = len(self.rgb_spaces) > 0
        self.use_multicam_fusion = len(self.rgb_spaces) > 1
        self.time_dim = list(self.observation_space.values())[0].shape[0]

        self.encoder = self.view_fusion = None
        self.build_encoder()
        self.build_view_fusion()
        self.build_reward_model()

        self.lr_scheduler = None
        if self.adaptive_lr:
            self.lr_scheduler = get_scheduler(
                name="cosine",
                optimizer=self.actor_opt,
                num_warmup_steps=100,
                num_training_steps=num_train_steps,
            )

    def build_encoder(self):
        rgb_spaces = extract_many_from_spec(
            self.observation_space, r"rgb.*", missing_ok=True
        )
        if len(rgb_spaces) > 0:
            rgb_shapes = [s.shape for s in rgb_spaces.values()]
            assert np.all(
                [sh == rgb_shapes[0] for sh in rgb_shapes]
            ), "Expected all RGB obs to be same shape."

            num_views = len(rgb_shapes)
            # Multiply first two dimensions to consider frame stacking
            obs_shape = (np.prod(rgb_shapes[0][:2]), *rgb_shapes[0][2:])
            self.encoder = self.encoder_model(input_shape=(num_views, *obs_shape))
            self.encoder.to(self.device)
            self.encoder_opt = None

    def build_view_fusion(self):
        self.rgb_latent_size = 0
        if not self.use_pixels:
            return
        if self.use_multicam_fusion:
            if self.view_fusion_model is None:
                logging.warn(
                    "Multicam fusion is enabled but view_fusion_model is not set!"
                )
                self.view_fusion_opt = None
                return

            self.view_fusion = self.view_fusion_model(
                input_shape=self.encoder.output_shape
            )
            self.view_fusion.to(self.device)
            self.view_fusion_opt = None
            if len([_ for _ in self.view_fusion.parameters()]) != 0:
                # Introduce optimizer when view_fusion_model is parametrized
                self.view_fusion_opt = torch.optim.Adam(
                    self.view_fusion.parameters(), lr=self.lr
                )
            self.rgb_latent_size = self.view_fusion.output_shape[-1]
        else:
            self.view_fusion = lambda x: x[:, 0]
            self.rgb_latent_size = self.encoder.output_shape[-1]

    def build_reward_model(self):
        input_shapes = self.get_fully_connected_inputs()
        input_shapes["actions"] = (np.prod(self.action_space.shape),)
        reward_model = self.reward_model(input_shapes=input_shapes)
        self.reward = MarkovianRewardModel(
            reward_model=reward_model,
            num_reward_models=self.num_reward_models,
            apply_final_layer_tanh=self.apply_final_layer_tanh,
        )
        self.reward.to(self.device)
        self.reward_opt = torch.optim.AdamW(
            self.reward.parameters(), lr=self.lr, weight_decay=self.weight_decay
        )

    def encode_rgb_feats(self, rgb, train=False):
        # (bs * seq *v, ch, h , w)
        bs, seq, v, c, h, w = rgb.shape
        image = rgb.transpose(1, 2).reshape(bs * v, seq, c, h, w)
        if train:
            image = self.aug(image.float())
        image = (
            image.reshape(bs, v, seq, c, h, w)
            .transpose(1, 2)
            .reshape(bs * seq, v, c, h, w)
        )
        # (bs * seq, v, ch, h , w)
        image = image.float().detach()

        with torch.no_grad():
            # (bs*seq, v, c, h, w) -> (bs*seq, v, h)
            multi_view_rgb_feats = self.encoder(image)
            # (bs*seq, v*h)
            fused_rgb_feats = self.view_fusion(multi_view_rgb_feats)
            # (bs, seq, v*h)
            fused_rgb_feats = fused_rgb_feats.view(*rgb.shape[:2], -1)
        return fused_rgb_feats

    @override
    def compute_reward(
        self,
        seq: Sequence,
        member: int = -1,
        return_reward: bool = False,
    ) -> torch.Tensor:
        """
        Compute the reward from sequences.

        Args:
            seq (Sequence): same with _episode_rollouts in workspace.py.
            observations (dict): Dictionary containing observations.
            actions (torch.Tensor): The actions taken.

        Returns:
            torch.Tensor: The reward tensor.

        """

        if not self.activated:
            return seq

        start_idx = 0

        if isinstance(seq, list):
            # seq: list of (action, obs, reward, term, trunc, info, next_info)
            actions = utils.convert_numpy_to_torch(
                np.stack([elem[0] for elem in seq]), self.device
            )
            if seq[0][1]["low_dim_state"].ndim > 1:
                list_of_obs_dicts = [
                    {k: v[-1] for k, v in elem[1].items()} for elem in seq
                ]
            else:
                list_of_obs_dicts = [elem[1] for elem in seq]

            obs = {key: [] for key in list_of_obs_dicts[0].keys()}
            for obs_dict in list_of_obs_dicts:
                for key, val in obs_dict.items():
                    obs[key].append(val)

            obs = utils.convert_numpy_to_torch(
                {key: np.stack(val) for key, val in obs.items()}, self.device
            )
            # obs: (T, elem_shape) for elem in obs
            # actions: (T, action_shape)
            if actions.ndim > 2 and actions.shape[-2] == 1:
                actions = actions[..., -1, :]

        elif isinstance(seq, dict):
            actions = utils.convert_numpy_to_torch(seq["action"], self.device)
            obs = utils.convert_numpy_to_torch(
                {
                    key: val[start_idx:]
                    for key, val in seq.items()
                    if key in self.observation_space.spaces
                },
                self.device,
            )
            if actions.ndim > 2 and actions.shape[-2] == 1:
                actions = actions[..., -1, :]
            #     obs = {
            #         key: utils.convert_numpy_to_torch(val[start_idx:, -1], self.device)
            #         for key, val in seq.items()
            #         if key in self.observation_space.spaces
            #     }

        if self.use_pixels:
            rgbs = (
                stack_tensor_dictionary(
                    extract_many_from_batch(obs, r"rgb(?!.*?tp1)"), 1
                )
                .unsqueeze(1)
                .to(self.device)
            )
            fused_rgb_feats = self.encode_rgb_feats(rgbs, train=False).squeeze(1)
        else:
            fused_rgb_feats = None
        qpos = (
            extract_from_batch(obs, "low_dim_state").to(self.device)
            if self.low_dim_size > 0
            else None
        )
        time_obs = (
            extract_from_batch(obs, "time", missing_ok=True).to(self.device)
            if self.time_obs_size > 0
            else None
        )

        # change components to be (bs * seq, -1)
        seq_len = None
        if qpos is not None and qpos.ndim > 2:
            qpos = qpos.reshape(-1, *qpos.shape[-1:])
        if fused_rgb_feats is not None:
            fused_rgb_feats = fused_rgb_feats.reshape(-1, *fused_rgb_feats.shape[-1:])
        if time_obs is not None:
            time_obs = time_obs.reshape(-1, *time_obs.shape[-1:])
        if actions.ndim > 2:
            seq_len = actions.shape[1]
            actions = actions.reshape(-1, *actions.shape[-1:])

        T = actions.shape[0] - start_idx
        rewards = []
        for i in trange(
            0,
            T,
            self.compute_batch_size,
            leave=False,
            ncols=0,
            desc="reward compute per batch",
        ):
            _range = list(range(i, min(i + self.compute_batch_size, T)))
            with torch.no_grad():
                if member == -1:
                    _reward = []
                    for mem in range(self.num_reward_models):
                        __reward = self.reward(
                            qpos[_range] if qpos is not None else None,
                            fused_rgb_feats[_range]
                            if fused_rgb_feats is not None
                            else None,
                            actions[_range],
                            time_obs[_range] if time_obs is not None else None,
                            member=mem,
                        )
                        _reward.append(__reward)
                    _reward = torch.cat(_reward, dim=1).mean(dim=1)
                else:
                    _reward = self.reward(
                        qpos[_range] if qpos is not None else None,
                        fused_rgb_feats[_range]
                        if fused_rgb_feats is not None
                        else None,
                        actions[_range],
                        time_obs[_range] if time_obs is not None else None,
                        member=member,
                    ).squeeze(-1)
                rewards.append(_reward)
        total_rewards = torch.cat(rewards, dim=0)
        assert (
            len(total_rewards) == T
        ), f"Expected {T} rewards, got {len(total_rewards)}"
        if seq_len is not None:
            total_rewards = total_rewards.view(-1, seq_len)

        if return_reward:
            return total_rewards

        total_rewards = total_rewards.cpu().numpy()
        if isinstance(seq, list):
            for idx in range(len(seq)):
                seq[idx][2] = total_rewards[idx]

        elif isinstance(seq, dict):
            seq["reward"] = total_rewards

        return seq

    def get_cropping_mask(self, r_hat, w):
        mask_ = []
        for i in range(w):
            B, S, _ = r_hat.shape
            length = np.random.randint(int(0.7 * S), int(0.9 * S) + 1, size=B)
            start_index = np.random.randint(0, S + 1 - length)
            mask = torch.zeros((B, S, 1)).to(self.device)
            for b in range(B):
                mask[b, start_index[b] : start_index[b] + length[b]] = 1
            mask_.append(mask)

        return torch.cat(mask_)

    @override
    def update(
        self, replay_iter, step: int, replay_buffer: ReplayBuffer = None
    ) -> dict:
        """
        Compute the loss from binary preferences.

        Args:
            replay_iter (iterable): An iterator over a replay buffer.
            step (int): The current step.
            replay_buffer (ReplayBuffer): The replay buffer.

        Returns:
            dict: Dictionary containing training metrics.

        """

        metrics = dict()
        loss_dict = defaultdict(float)

        for member in range(self.num_reward_models):
            batch = next(replay_iter)
            batch = {k: v.to(self.device) for k, v in batch.items()}
            r_hats = []

            for i in range(2):
                # (bs, seq, action_shape)
                actions = batch[f"seg{i}_action"]
                if self.low_dim_size > 0:
                    # (bs, seq, low_dim)
                    qpos = extract_from_batch(batch, f"seg{i}_low_dim_state").detach()

                if self.use_pixels:
                    # (bs, seq, v, ch, h, w)
                    rgb = stack_tensor_dictionary(
                        extract_many_from_batch(batch, rf"seg{i}_rgb(?!.*?tp1)"), 2
                    )
                    fused_rgb_feats = self.encode_rgb_feats(rgb, train=True)
                else:
                    fused_rgb_feats = None

                time_obs = extract_from_batch(batch, "time", missing_ok=True)
                r_hat_segment = self.reward(
                    qpos.reshape(-1, *qpos.shape[2:]),
                    fused_rgb_feats.reshape(-1, *fused_rgb_feats.shape[2:])
                    if fused_rgb_feats is not None
                    else None,
                    actions.reshape(-1, *actions.shape[2:]),
                    time_obs.reshape(-1, *time_obs.shape[2:])
                    if time_obs is not None
                    else None,
                    member=member,
                )
                r_hat = r_hat_segment.view(
                    *actions.shape[:-2], -1, r_hat_segment.shape[-1]
                )
                if self.data_aug_ratio > 0.0:
                    mask = self.get_cropping_mask(r_hat, self.data_aug_ratio)
                    r_hat = r_hat.repeat(self.data_aug_ratio, 1, 1)
                    r_hat = (mask * r_hat).sum(axis=-2)
                else:
                    r_hat = r_hat_segment.sum(dim=-2)
                r_hats.append(r_hat)

            labels = batch["label"]
            if self.data_aug_ratio > 0:
                labels = labels.repeat(self.data_aug_ratio, 1)

            _loss_dict = self.reward.calculate_loss(r_hats, labels)
            for k, v in _loss_dict.items():
                loss_dict[k] += v

        for i in range(self.num_labels):
            loss_dict[f"pref_acc_label_{i}"] /= self.num_reward_models

        # calculate gradient
        if self.use_pixels and self.encoder_opt is not None:
            self.encoder_opt.zero_grad(set_to_none=True)
            if self.use_multicam_fusion and self.view_fusion_opt is not None:
                self.view_fusion_opt.zero_grad(set_to_none=True)
        self.reward_opt.zero_grad(set_to_none=True)
        loss_dict["loss"].backward()

        # step optimizer
        if self.use_pixels and self.encoder_opt is not None:
            self.encoder_opt.step()
            if self.use_multicam_fusion and self.view_fusion_opt is not None:
                self.view_fusion_opt.step()
        self.reward_opt.step()

        # step lr scheduler every batch
        # this is different from standard pytorch behavior
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()

        if self.logging:
            metrics["reward_loss"] = loss_dict["loss"].item()
            for label in range(self.num_labels):
                metrics[f"pref_acc_label_{label}"] = loss_dict[
                    f"pref_acc_label_{label}"
                ].item()

        return metrics

    def reset(self, step: int, agents_to_reset: list[int]):
        pass  # TODO: Implement LSTM support.
