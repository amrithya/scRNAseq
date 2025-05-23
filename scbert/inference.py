import os
import argparse
import numpy as np
import pickle as pkl
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
from performer_pytorch import PerformerLM
import scanpy as sc
from sklearn.model_selection import StratifiedShuffleSplit
from sklearn.metrics import confusion_matrix, accuracy_score, f1_score
import random

parser = argparse.ArgumentParser()
parser.add_argument("--local_rank", "--local-rank", type=int, default=-1)
parser.add_argument("--bin_num", type=int, default=5)
parser.add_argument("--gene_num", type=int, default=16906)
parser.add_argument("--epoch", type=int, default=100)
parser.add_argument("--seed", type=int, default=2021)
parser.add_argument("--batch_size", type=int, default=3)
parser.add_argument("--pos_embed", type=bool, default=True)
parser.add_argument("--data_path", type=str, default='./data/Zheng68K.h5ad')
parser.add_argument("--model_path", type=str, default='./panglao_pretrained.pth')
parser.add_argument("--ckpt_dir", type=str, default='./ckpts/')
parser.add_argument("--model_name", type=str, default='finetune_ready')
args = parser.parse_args()

print(f"Arguments: {args}")

random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
print(f"Random seeds set to {args.seed}")

local_rank = args.local_rank
is_master = local_rank == 0
print(f"Local rank: {local_rank}, is_master: {is_master}")

dist.init_process_group(backend='nccl')
print("Initialized distributed process group with NCCL backend")

torch.cuda.set_device(local_rank)
device = torch.device("cuda", local_rank)
print(f"Using device: {device}")

CLASS = args.bin_num + 2
SEQ_LEN = args.gene_num + 1
print(f"CLASS: {CLASS}, SEQ_LEN: {SEQ_LEN}")

class SCDataset(Dataset):
    def __init__(self, data, label):
        self.data = data
        self.label = label

    def __getitem__(self, index):
        full_seq = self.data[index].toarray()[0]
        full_seq[full_seq > (CLASS - 2)] = CLASS - 2
        full_seq = torch.from_numpy(full_seq).long()
        full_seq = torch.cat((full_seq, torch.tensor([0])))
        return full_seq, self.label[index]

    def __len__(self):
        return self.data.shape[0]

class Identity(nn.Module):
    def __init__(self, dropout=0., h_dim=100, out_dim=10):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 1, (1, 200))
        self.act = nn.ReLU()
        self.fc1 = nn.Linear(SEQ_LEN, 512)
        self.act1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)
        self.fc2 = nn.Linear(512, h_dim)
        self.act2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)
        self.fc3 = nn.Linear(h_dim, out_dim)
        self._printed = False

    def forward(self, x):
        if not self._printed:
            print(f"Shape after Performer, before Identity: {x.shape}")
        x = x[:, None, :, :]
        x = self.conv1(x)
        x = self.act(x)
        x = x.view(x.shape[0], -1)
        x = self.fc1(x)
        x = self.act1(x)
        x = self.dropout1(x)
        x = self.fc2(x)
        x = self.act2(x)
        x = self.dropout2(x)
        x = self.fc3(x)
        if not self._printed:
            print(f"Shape after Identity: {x.shape}")
            self._printed = True
        return x

print("Loading dataset from:", args.data_path)
data = sc.read_h5ad(args.data_path)
print(f"Dataset loaded: {data}")

label_dict, label = np.unique(data.obs['celltype'], return_inverse=True)
print(f"Unique cell types found: {len(label_dict)}")
label = torch.from_numpy(label)
data = data.X

os.makedirs(args.ckpt_dir, exist_ok=True)
with open(os.path.join(args.ckpt_dir, 'label_dict.pkl'), 'wb') as f:
    pkl.dump(label_dict, f)
print(f"Saved label_dict.pkl to {args.ckpt_dir}")

with open(os.path.join(args.ckpt_dir, 'label.pkl'), 'wb') as f:
    pkl.dump(label, f)
print(f"Saved label.pkl to {args.ckpt_dir}")

sss = StratifiedShuffleSplit(n_splits=1, test_size=0.2, random_state=args.seed)
for train_idx, val_idx in sss.split(data, label):
    print(f"Train/val split: {len(train_idx)} train, {len(val_idx)} val")
    train_dataset = SCDataset(data[train_idx], label[train_idx])
    val_dataset = SCDataset(data[val_idx], label[val_idx])

train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0, pin_memory=True)
val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=True)
print(f"Created DataLoaders with batch size {args.batch_size}")

model = PerformerLM(
    num_tokens=CLASS,
    dim=200,
    depth=6,
    max_seq_len=SEQ_LEN,
    heads=10,
    g2v_position_emb=args.pos_embed
)
print("PerformerLM model instantiated")

ckpt = torch.load(args.model_path, map_location='cpu')
print(f"Loaded checkpoint from {args.model_path}")
model.load_state_dict(ckpt['model_state_dict'])
print("Loaded model state dict")

model.to_out = Identity(dropout=0., h_dim=128, out_dim=len(label_dict))
model.add_module("to_out", model.to_out)

for param in model.parameters():
    param.requires_grad = False
print("Set all model parameters to requires_grad=False")

model.to(device)
model.eval()
print("Model moved to device and set to eval mode")

criterion = nn.CrossEntropyLoss()

all_preds = []
all_labels = []

with torch.no_grad():
    print("Starting validation loop...")
    for batch_idx, (x_val, y_val) in enumerate(val_loader):
        x_val, y_val = x_val.to(device), y_val.to(device)
        print(f"Batch {batch_idx} - x_val shape: {x_val.shape}, y_val shape: {y_val.shape}")
        logits = model(x_val)
        preds = logits.argmax(dim=1)
        all_preds.append(preds.cpu())
        all_labels.append(y_val.cpu())
    print("Validation loop completed")

all_preds = torch.cat(all_preds).numpy()
all_labels = torch.cat(all_labels).numpy()

acc = accuracy_score(all_labels, all_preds)
f1 = f1_score(all_labels, all_preds, average='weighted')

print("\nValidation Results on Full Dataset:")
print(f"Accuracy: {acc:.4f}")
print(f"F1 Score (weighted): {f1:.4f}")
print("Confusion matrix:")
print(confusion_matrix(all_labels, all_preds))
