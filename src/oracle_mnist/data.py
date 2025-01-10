import os
import shutil
import tarfile
from hashlib import sha256
from pathlib import Path
from typing import Literal, Optional
from abc import ABC, abstractmethod

import tqdm
import gdown
import numpy as np
import cv2
from PIL import Image
from skimage.filters import threshold_otsu

import torch
import pytorch_lightning as pl
from torch.utils.data import DataLoader, Dataset


RAW_DATA_PATH = './data/raw/'
ONLINE_DATA_URL = "https://drive.google.com/uc?id=1gPYAOc9CTvrUQFCASW3oz30lGdKBivn5"


class OracleMNIST(Dataset):
    """
    Oracle MNIST dataset

    Loads the data from disk
    """

    def __init__(
        self,
        data_paths: list[Path],
        use_rgb: bool = True
    ) -> None:

        super().__init__()
        self.data_paths = sorted(data_paths)
        self.use_rgb = use_rgb

    def __len__(self) -> int:
        return len(self.data_paths)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.LongTensor]:
        path = self.data_paths[idx]
        data = torch.from_numpy(np.load(path)).float()
        target = int(path.parent.name)
        return data.repeat(3, 1, 1) if self.use_rgb else data, torch.tensor(target).long()


class OracleMNISTInMemory(OracleMNIST):
    """
    Oracle MNIST dataset

    Loads the data from memory
    """

    def __init__(self, data_paths):
        super().__init__(data_paths)

        self.data = [None] * len(self.data_paths)
        self.targets = [None] * len(self.data_paths)
        for idx in range(len(self.data_paths)):
            self.data[idx], self.targets[idx] = super().__getitem__(idx)
        self.data = torch.stack(self.data)
        self.targets = torch.stack(self.targets)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.LongTensor]:
        self.data[idx], self.targets[idx]


class OracleMNISTBaseModule(ABC, pl.LightningDataModule):
    """
    This is the base class for the Oracle MNIST dataset

    It provides the basic functionality for the dataset
    """

    train_dataset: Optional[torch.Tensor] = None
    val_dataset: Optional[torch.Tensor] = None
    test_dataset: Optional[torch.Tensor] = None
    data_version_name: str

    def __init__(
        self,
        val_split: float = 0.2,
        in_memory_dataset: bool = False,
        use_rgb: bool = True,
        **dataloader_kwargs
    ) -> None:

        super().__init__()

        _dir = Path(RAW_DATA_PATH)
        self.raw_train_dir = _dir / 'train'
        self.raw_test_dir = _dir / 'test'

        self.processed_train_dir = _dir.with_name('processed') / '{data_version_name}' / 'train'
        self.processed_test_dir = _dir.with_name('processed') / '{data_version_name}' / 'test'

        self.val_split = val_split
        self.in_memory_dataset = in_memory_dataset
        self.use_rgb = use_rgb
        self.dataloader_kwargs = dataloader_kwargs

        self.post_init()

    def post_init(self) -> None:
        """
        Post initialization method
        """
        self._format_data_version(self.data_version_name)

    @staticmethod
    def _download_data() -> None:
        """
        Download and extraxt raw data
        """
        _tmp_path = os.path.join(RAW_DATA_PATH, 'raw.tar.gz')
        _tmp_dir = Path(RAW_DATA_PATH) / 'oracle-mnist-origin'

        # download
        gdown.download(ONLINE_DATA_URL, _tmp_path)

        # extract
        with tarfile.open(_tmp_path, "r:gz") as tar:
            tar.extractall(RAW_DATA_PATH)

        # move
        for _path in _tmp_dir.iterdir():
            shutil.move(str(_path), RAW_DATA_PATH)

        # cleanup
        os.rmdir(_tmp_dir)
        os.remove(_tmp_path)

    def _format_data_version(self, data_version_name: str) -> None:
        """
        Change the data version name in the path
        """
        self.processed_train_dir = Path(
            str(self.processed_train_dir).format(data_version_name=data_version_name))
        self.processed_test_dir = Path(
            str(self.processed_test_dir).format(data_version_name=data_version_name))

    def prepare_data(self) -> None:
        """
        Prepare the data, this is run once in the beginning of training
        """
        if not self.raw_train_dir.exists() or not self.raw_test_dir.exists():
            self._download_data()
        self._process_data()

    def _process_data(self) -> None:
        """
        1) Check if the data has been preprocessed
        2) If not, preprocess the data
        """
        if self.processed_train_dir.exists() and self.processed_test_dir.exists():
            return
        
        in_dirs = (self.raw_train_dir, self.raw_test_dir)
        out_dirs = (self.processed_train_dir, self.processed_test_dir)

        for in_dir, out_dir in tqdm.tqdm(list(zip(in_dirs, out_dirs)), desc='Processing dataset'):

            for in_path in tqdm.tqdm(list(in_dir.glob('**/*.bmp')), desc='Processing data'):
                out_path = out_dir / in_path.relative_to(in_dir).with_suffix('.npy')
                out_path.parent.mkdir(parents=True, exist_ok=True)
                im = np.asarray(
                    Image.open(in_path).convert('L'),
                    dtype=np.uint8)[..., None]
                np.save(
                    out_path,
                    np.moveaxis(self.process_datapoint(im), -1, 0))

    def _is_val_data(self, datapoint_path: Path) -> bool:
        """
        This is used for a persistent test/val split
        """
        rel_path = str(datapoint_path.relative_to(self.processed_train_dir))
        sha256_hash = sha256(rel_path.encode()).hexdigest()
        return int(sha256_hash, 16) % 100 < self.val_split * 100

    @abstractmethod
    def process_datapoint(self, data: np.ndarray) -> np.ndarray:
        """
        Does whatever we want to do with a datapoint

        input shape is (H, W, C)
        """

    def setup(
        self,
        stage: Literal['fit', 'validate', 'test', 'predict']
    ) -> None:
        """
        This is run once on each process in distributed training
        """
        
        _dataset = OracleMNISTInMemory if self.in_memory_dataset else OracleMNIST

        # val dataset
        if stage in ('fit', 'validate'):
            data_paths = np.array(sorted(self.processed_train_dir.glob('**/*.npy')))
            is_val_data = np.array([self._is_val_data(path) for path in data_paths])

            self.val_dataset = _dataset(
                data_paths=data_paths[is_val_data],
                in_memory=self.in_memory_dataset,
                use_rgb=self.use_rgb)
        
        # train dataset
        if stage == 'fit':
            self.train_dataset = _dataset(
                data_paths=data_paths[~is_val_data],
                in_memory=self.in_memory_dataset,
                use_rgb=self.use_rgb)

        # test dataset
        if stage in ('test', 'predict'):
            self.test_dataset = _dataset(
                data_paths=sorted(self.processed_test_dir.glob('**/*.npy')),
                in_memory=self.in_memory_dataset,
                use_rgb=self.use_rgb)

    def train_dataloader(self) -> DataLoader:
        """
        Returns the training dataloader
        """
        kwargs = dict(shuffle=True) | self.dataloader_kwargs
        return DataLoader(
            self.train_dataset,
            **kwargs)
    
    def val_dataloader(self) -> DataLoader:
        """
        returns the validation dataloader
        """
        kwargs = dict(shuffle=False) | self.dataloader_kwargs
        return DataLoader(
            self.val_dataset,
            **kwargs)
    
    def test_dataloader(self) -> DataLoader:
        """
        returns the test dataloader
        """
        kwargs = dict(shuffle=False) | self.dataloader_kwargs
        return DataLoader(
            self.test_dataset,
            **kwargs)


class OracleMNISTModuleBasic(OracleMNISTBaseModule):
    """
    This data module mimics the preprocessing steps of the original paper.

    https://arxiv.org/abs/2205.09442
    """

    data_version_name = 'basic_{imsize}'

    def __init__(self, *args, imsize: int=28, **kwargs) -> None:
        self.data_version_name = self.data_version_name.format(imsize=imsize)
        super().__init__(*args, **kwargs)

        self.im_size = imsize

    def process_datapoint(self, data: np.ndarray) -> np.ndarray:
        
        # Negating the intensities of the image if its foreground is darker than the background.
        thresh = threshold_otsu(data)
        if 0.5 < (thresh < data).mean():
            data = 255 - data

        # Resizing the longest edge of the image to self.im_size using a bi-cubic interpolation algorithm.
        h, w, c = data.shape
        if h > w:
            new_h, new_w = self.im_size, round(self.im_size * w / h)
        else:
            new_h, new_w = round(self.im_size * h / w), self.im_size
        data = cv2.resize(data, (new_w, new_h), interpolation=cv2.INTER_CUBIC)[..., None]

        # Extending the shortest edge to self.im_size and put the image to the center of the canvas.
        h, w, c = data.shape
        pad_h = (self.im_size - h)
        pad_w = (self.im_size - w)
        data = np.pad(
            data,
            (
                (
                    pad_h // 2,
                    pad_h - pad_h // 2),
                (
                    pad_w // 2,
                    pad_w - pad_w // 2),
                (
                    0,
                    0)),
            mode='constant')
        
        return data


if __name__ == "__main__":

    data_module = OracleMNISTModuleBasic(batch_size=32)
    data_module.prepare_data()
    data_module.setup('fit')
    data_module.setup('validate')
    data_module.setup('test')

    train_loader = data_module.train_dataloader()
    val_loader = data_module.val_dataloader()
    test_loader = data_module.test_dataloader()

    print(len(train_loader))
    print(len(val_loader))
    print(len(test_loader))
