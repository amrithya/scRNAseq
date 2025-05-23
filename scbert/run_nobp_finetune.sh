#!/bin/bash

#SBATCH --job-name="scBERT_nobp_finetune"          # Job Name

#SBATCH --partition=gpu                     # Partition name

#SBATCH --gres=gpu:2

#SBATCH --ntasks=2

#SBATCH --cpus-per-task=3

#SBATCH --hint=nomultithread

#SBATCH --time=08:00:00                     # Max runtime (HH:MM:SS)

#SBATCH --output=results/scBERT_nobp_finetune_%A_%a.out    # STDOUT file

#SBATCH --error=results/scBERT_nobp_finetune_%A_%a.err     # STDERR file

CUDA_LAUNCH_BLOCKING=1 poetry run python -u -m torch.distributed.launch --nproc_per_node=2 finetune_nobp.py \
          --data_path "/data1/data/corpus/Zheng68K.h5ad" \
            --model_path "/data1/data/corpus/panglao_pretrain.pth"

echo "All Done!"
wait
