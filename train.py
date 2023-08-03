import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, random_split
from torch.optim import SGD
from torch.cuda.amp.grad_scaler import GradScaler
from pathlib import Path
import random

from voc2012 import VOC2012Dataset
from model import DeepLabv3ResNet101
from loss import DeepLabLoss
from evaluate import PixelmIoU
from utils import get_device

# "We decouple the DCNN and CRF training stages, assuming the DCNN unary terms are fixed
# when setting the CRF parameters."


def get_lr(step, n_steps, power=0.9):
    # "We employ a 'poly' learning rate policy where the initial learning rate is multiplied
    # by $1 - \frac{iter}{max_iter}^{power}$ with $power = 0.9$."
    lr = 1 - (step / n_steps) ** power
    return lr


ROOT_DIR = Path(__file__).parent
# "Since large batch size is required to train batch normalization parameters, we employ `output_stride=16`
# and compute the batch normalization statistics with a batch size of 16. The batch normalization parameters
# are trained with $decay = 0.9997$. After training on the 'trainaug' set with 30K iterations
# and $initial learning rate = 0.007$, we then freeze batch normalization parameters,
# employ `output_stride = 8`, and train on the official PASCAL VOC 2012 trainval set
# for another 30K iterations and smaller $base learning rate = 0.001$."
IMG_SIZE = 513
N_EPOCHS = 50
BATCH_SIZE = 16
# N_WORKERS = 4
N_WORKERS = 0
# IMG_DIR = "/Users/jongbeomkim/Documents/datasets/voc2012/VOCdevkit/VOC2012/JPEGImages"
# GT_DIR = "/Users/jongbeomkim/Documents/datasets/SegmentationClassAug"
IMG_DIR = "/home/user/cv/voc2012/VOCdevkit/VOC2012/JPEGImages"
GT_DIR = "/home/user/cv/SegmentationClassAug"
LR = 0.0007
# MOMENTUM = 0.9
WEIGHT_DECAY = 0.0005

DEVICE = get_device()
model = DeepLabv3ResNet101(output_stride=16).to(DEVICE)
model = nn.DataParallel(model, output_device=0)
optim = SGD(params=model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
scaler = GradScaler()

train_ds = VOC2012Dataset(img_dir=IMG_DIR, gt_dir=GT_DIR, split="train")
train_dl = DataLoader(
    train_ds, batch_size=BATCH_SIZE, shuffle=True, num_workers=N_WORKERS, pin_memory=True, drop_last=True
)
train_di = iter(train_dl)

val_ds = VOC2012Dataset(img_dir=IMG_DIR, gt_dir=GT_DIR, split="val")
val_dl = DataLoader(val_ds, batch_size=1, shuffle=True, num_workers=N_WORKERS)

crit = DeepLabLoss()
metric = PixelmIoU()

### Train.
N_STEPS = 300_000
running_loss = 0
for step in range(1, N_STEPS + 1):
    model.train()

    try:
        image, gt = next(train_di)
    except StopIteration:
        train_di = iter(train_dl)
        image, gt = next(train_di)
    image = image.to(DEVICE)
    gt = gt.to(DEVICE)

    lr = get_lr(step=step, n_steps=N_STEPS)
    optim.param_groups[0]["lr"] = lr

    optim.zero_grad()

    with torch.autocast(device_type=DEVICE.type, dtype=torch.float16):
        pred = model(image)
    
    loss = crit(pred=pred, gt=gt)
    scaler.scale(loss).backward()
    scaler.step(optim)
    scaler.update()
    # loss.backward()
    # optim.step()

    running_loss += loss.item()

    if step % 100 == 0:
        running_loss /= 100
        print(f"""[ {step}/{N_STEPS} ][ {lr} ] Loss: {running_loss:.4f}""")

    ### Evaluate.
    if step % 1000 == 0:
        model.eval()
        with torch.no_grad():
            for image, gt in val_dl:
                pred = model(image)
                miou = metric(pred=pred, gt=gt)
                print(f"""[ {step}/{N_STEPS} ][ {lr} ] mIoU: {miou:.4f}""")
