# Copyright (2024) Bytedance Ltd. and/or its affiliates

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import imageio
import os
import argparse
import torch
import json
import numpy as np
from copy import deepcopy
from imageio import get_writer
from einops import rearrange
from tqdm import tqdm
from diffusers.models import AutoencoderKL

from models import get_models
from dataset import get_dataset
from util import (get_args, requires_grad)
from evaluate.generate_short_video import generate_single_video

def create_arrow_image(direction='w', size=50, color=(255, 0, 0)):
    """
    Create an arrow image pointing in the specified direction.
    """
    image = np.zeros((size, size, 3), dtype=np.uint8)
    image_path = './sample/arrow.jpg'
    print(f"Reading arrow image from: {image_path}")
    image = imageio.imread(image_path)
    
    if direction == 's':
        image = np.flipud(image)
    elif direction == 'a':
        image = np.rot90(image)
    elif direction == 'd':
        image = np.rot90(image, -1)
    
    return image

def add_arrows_to_video(video_np, save_path, actions):
    """
    Add direction arrows to the video based on the actions list and save the modified video.
    """
    if len(actions) != video_np.shape[0]:
        raise ValueError("The length of the actions list must match the number of video frames.")
    
    print(f"Saving modified video with arrows to: {save_path}")
    writer = get_writer(save_path, fps=4) 
    for frame, action in zip(video_np, actions):
        if action != ' ':
            arrow_img = create_arrow_image(direction=action)
            position = (200, 240)
            for i in range(arrow_img.shape[0]):
                for j in range(arrow_img.shape[1]):
                    if np.any(arrow_img[i, j] != 0): 
                        frame[position[0]+i, position[1]+j] = arrow_img[i, j]
        writer.append_data(frame)
    writer.close()

def main(args):
    assert torch.cuda.is_available(), "Training currently requires at least one GPU."
    device = torch.device("cuda", 0)

    args.latent_size = [t // 8 for t in args.video_size]
    model = get_models(args)
    ema = deepcopy(model).to(device) 
    requires_grad(ema, False)
    vae = AutoencoderKL.from_pretrained(args.vae_model_path, subfolder="vae").to(device)

    if args.evaluate_checkpoint:
        print(f"Loading model checkpoint from: {args.evaluate_checkpoint}")
        checkpoint = torch.load(args.evaluate_checkpoint, map_location=lambda storage, loc: storage)
        if "ema" in checkpoint: 
            print('Using ema ckpt!')
            checkpoint = checkpoint["ema"]

        model_dict = model.state_dict()
        pretrained_dict = {}
        for k, v in checkpoint.items():
            if k in model_dict:
                pretrained_dict[k] = v
            else:
                print(f'Ignoring: {k}')
        print(f'Successfully loaded {len(pretrained_dict) / len(checkpoint.items()) * 100}% of the original pretrained model weights.')
        model_dict.update(pretrained_dict)
        model.load_state_dict(model_dict)
        print(f'Successfully loaded model from: {args.evaluate_checkpoint}') 
    model.to(device)
    model.eval()

    train_dataset, val_dataset = get_dataset(args)

    ann_file = val_dataset.ann_files[0]
    print(f"Reading annotation file from: {ann_file}")

    with open(ann_file, "rb") as f:
        ann = json.load(f)

    latent_video_path = os.path.join(args.video_path, ann['latent_video_path'])
    print(f"Loading latent video from: {latent_video_path}")
    with open(latent_video_path, 'rb') as f:
        latent_video = torch.load(f)['obs']
    
    video_path = os.path.join(args.video_path, ann['video_path'])
    print(f"Loading video from: {video_path}")
    video_reader = imageio.get_reader(video_path)
    
    video_tensor = []
    for frame in video_reader:
        frame_tensor = torch.tensor(frame)
        video_tensor.append(frame_tensor)
    video_reader.close()
    video_tensor = torch.stack(video_tensor)

    game_dir = 'application/languagetable_game'
    print(f"Creating game directory: {game_dir}")
    os.makedirs(game_dir, exist_ok=True)

    start_idx = 0
    start_image = latent_video[start_idx]
    video_tensor = video_tensor[start_idx:]

    video_tensor = video_tensor.permute(0, 3, 1, 2)
    video_tensor = val_dataset.resize_preprocess(video_tensor)
    video_tensor = video_tensor.permute(0, 2, 3, 1)

    first_image_path = os.path.join(game_dir, 'first_image.png')
    print(f"Saving the first image of the video to: {first_image_path}")
    imageio.imwrite(first_image_path, video_tensor[0].numpy())

    seg_video_list = [video_tensor[0:1].numpy()]
    seg_idx = 0

    while True:
        env_actions = read_actions_from_keyboard()
        actions = []
        for action in env_actions:
            if action == 'd':
                actions.append([0, 0.5])
            elif action == 'a':
                actions.append([0, -0.5])
            elif action == 's':
                actions.append([-0.5, 0])
            elif action == 'w':
                actions.append([0.5, 0])
            else:
                actions.append([0, 0])
        
        print(f'Processing actions: {" ".join(env_actions)}')
        actions = torch.from_numpy(np.array(actions))

        start_image = start_image.unsqueeze(0).unsqueeze(0)
        seg_action = actions.unsqueeze(0)
        seg_video, seg_latents = generate_single_video(args, start_image, seg_action, device, vae, model)

        start_image = seg_latents.squeeze()[-1].clone()

        t_videos = ((seg_video / 2.0 + 0.5).clamp(0, 1) * 255).detach().to(dtype=torch.uint8).cpu().contiguous()
        t_videos = rearrange(t_videos, 'f c h w -> f h w c').numpy()

        seg_video_list.append(t_videos[1:])
        all_video = np.concatenate(seg_video_list, axis=0)

        output_video_path = os.path.join(game_dir, f'all_{seg_idx}-th.mp4')
        print(f"Saving generated video to: {output_video_path}")
        writer = get_writer(output_video_path, fps=4)
        for frame in all_video:
            writer.append_data(frame)
        writer.close()

        print(f"Generated video: {output_video_path}")
        seg_idx += 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./configs/evaluation/languagetable/frame_ada.yaml")
    args = parser.parse_args()
    args = get_args(args)
    main(args)


"""
the script reads from:

Annotation files: languagetable/annotation/val/
Latent video files: languagetable/evaluation_latent_videos/
Video files: languagetable/evaluation_videos/
Model checkpoints: languagetable/checkpoints/
An arrow image: ./sample/arrow.jpg

And it writes output files to:
application/languagetable_game/
"""
