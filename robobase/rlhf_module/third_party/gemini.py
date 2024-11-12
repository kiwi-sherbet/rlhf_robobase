import logging
import os
import time

import google.generativeai as genai


from robobase.rlhf_module.utils import retry_on_error
from robobase.utils import read_video_from_bytes


def load_model(cfg):
    api_key = os.getenv("GEMINI_API_KEY")

    genai.configure(api_key=api_key)
    generation_config = {
        "temperature": cfg.temperature,
        "top_p": cfg.top_p,
        "top_k": cfg.top_k,
        "max_output_tokens": cfg.max_output_tokens,
    }

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    model = genai.GenerativeModel(
        model_name=cfg.model_type,
        generation_config=generation_config,
        safety_settings=safety_settings,
    )
    return model


@retry_on_error(10)
def upload_video_to_genai(video_path):
    video_file = genai.upload_file(path=video_path)
    while video_file.state.name == "PROCESSING":
        logging.info("Waiting for video to be processed.")
        time.sleep(3)
        video_file = genai.get_file(video_file.name)
    if video_file.state.name == "FAILED":
        raise ValueError(video_file.state.name)
    logging.info("Video processing complete: " + video_file.uri)
    return video_file


def postprocess_gemini_response(response):
    """
    Response format:
    <Answer>: Video 1
    """
    text = response.text
    try:
        stripped_text = text[:17]
        postprocessed_text = int(stripped_text.split(":")[1].strip().split(" ")[-1])
        return postprocessed_text
    except Exception as e:
        print(f"Error in postprocessing: {text} / {e}")
        return "Equally preferred"


def get_gemini_video_ids(segments, idx, target_viewpoints):
    video_files = [
        read_video_from_bytes(segments[f"gemini_video_path_{target_viewpoint}"][idx])
        for target_viewpoint in target_viewpoints
    ]
    return [genai.get_file(video_file) for video_file in video_files]
