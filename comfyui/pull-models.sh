#!/bin/sh
# Download Qwen-Image 2.1's files for ComfyUI into the models volume. Finished files
# are skipped; a half-downloaded one (.part) resumes where it stopped.
set -eu

BASE=https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main
# folder in ComfyUI's models directory, then the file's path in the repository.
FILES="
diffusion_models diffusion_models/qwen_image_2.1_turbo_bf16.safetensors
text_encoders text_encoders/qwen3vl_8b_int8_convrot.safetensors
vae vae/qwen_image_2.1_vae_bf16.safetensors
"

echo "$FILES" | while read -r folder path; do
    [ -n "$folder" ] || continue
    target="/models/$folder/$(basename "$path")"
    if [ -f "$target" ]; then
        echo "Have $target"
        continue
    fi
    mkdir -p "/models/$folder"
    echo "Downloading $path"
    curl -fL --retry 5 --retry-delay 5 -C - -o "$target.part" "$BASE/$path"
    mv "$target.part" "$target"
done
echo "Image model ready"
