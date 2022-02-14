from abc import abstractmethod
from functools import partial
import matplotlib.pyplot as plt

import torch.nn.functional as F
import numpy as np
from pytorch_lightning import LightningModule, Trainer
from torch.utils.data import DataLoader, TensorDataset, random_split
from torchts.nn.loss import quantile_err


class TimeSeriesModel(LightningModule):
    """Base class for all TorchTS models.

    Args:
        optimizer (torch.optim.Optimizer): Optimizer
        opimizer_args (dict): Arguments for the optimizer
        criterion: Loss function
        criterion_args (dict): Arguments for the loss function
        method: conformal prediction 
        scheduler (torch.optim.lr_scheduler): Learning rate scheduler
        scheduler_args (dict): Arguments for the scheduler
        scaler (torchts.utils.scaler.Scaler): Scaler
    """

    def __init__(
        self,
        optimizer,
        optimizer_args=None,
        criterion=F.mse_loss,
        criterion_args=None,
        significance=None,
        method=None,
        scheduler=None,
        scheduler_args=None,
        scaler=None,
    ):
        super().__init__()
        self.criterion = criterion
        self.criterion_args = criterion_args
        self.significance = significance
        self.method = method
        self.scaler = scaler

        if optimizer_args is not None:
            self.optimizer = partial(optimizer, **optimizer_args)
        else:
            self.optimizer = optimizer

        if scheduler is not None and scheduler_args is not None:
            self.scheduler = partial(scheduler, **scheduler_args)
        else:
            self.scheduler = scheduler

    def fit(self, x, y, max_epochs=10, batch_size=128):
        """Fits model to the given data.

        Args:
            x (torch.Tensor): Input data
            y (torch.Tensor): Output data
            max_epochs (int): Number of training epochs
            batch_size (int): Batch size for torch.utils.data.DataLoader
        """
        dataset = TensorDataset(x, y)
        # loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
        # trainer.fit(self, loader)

        # split data into train val test (will change after Dataloader set TODO)
        # data_split = [0.6,0.2,0.2]
        # lengths = [int(len(dataset)*0.6), int(len(dataset)*0.2), int(len(dataset)*0.2)]
        lengths = [int(len(dataset)*0.6), int(len(dataset)*0.8)]
        self.train_dataset, self.val_dataset, self.test_dataset = dataset[:lengths[0]], dataset[lengths[0]:lengths[1]], dataset[lengths[1]:]
        
        train_dataloader = DataLoader(self.train_dataset, batch_size=batch_size, shuffle=True)
        val_dataloader = DataLoader(self.val_dataset, batch_size=batch_size, shuffle=False)
        test_dataloader = DataLoader(self.test_dataset, batch_size=batch_size, shuffle=False)
        # split to only train on training set
        self.trainer = Trainer(max_epochs=max_epochs)
        
        # self.trainer.fit(self, train_dataloader, val_dataloader)
        self.trainer.fit(self, train_dataloader)

    def prepare_batch(self, batch):
        if self.scaler is not None:
            batch = self.scaler,fit_transform(batch)
        return batch

    def _step(self, batch, batch_idx, num_batches):
        """

        Args:
            batch: Output of the torch.utils.data.DataLoader
            batch_idx: Integer displaying index of this batch
            dataset: Data set to use

        Returns: loss for the batch
        """
        x, y = self.prepare_batch(batch)
        if self.training:
            batches_seen = batch_idx + self.current_epoch * num_batches
        else:
            batches_seen = batch_idx

        pred = self(x, y, batches_seen)

        if self.scaler is not None:
            y = self.scaler.inverse_transform(y)
            pred = self.scaler.inverse_transform(pred)

        if self.criterion_args is not None:
            if (not self.training) and self.method=='conformal':
                intervals = np.zeros((x.shape[0], 3))
                # ensure that we want to multiply our error distances by the size of our training set
                err_dist = np.hstack([self.err_dist] * x.shape[0])

                intervals[:, 0] = pred[:, 0] - err_dist[0, :]
                intervals[:, 1] = pred[:, 1]
                intervals[:, -1] = pred[:, -1] + err_dist[1, :]
                loss = self.criterion(intervals, y, **self.criterion_args)
            else:
                loss = self.criterion(pred, y, **self.criterion_args)
        else:
            loss = self.criterion(pred, y)

        return loss
    
    def calibration(self, batch, batch_idx, num_batches):
        """

        Args:
            batch: Output of the torch.utils.data.DataLoader
            batch_idx: Integer displaying index of this batch
            dataset: Data set to use

        Returns: err_dist for the calibration set
        """
        x, y = self.prepare_batch(batch)
        batches_seen = batch_idx
        pred = self(x, y, batches_seen)

        if self.scaler is not None:
            y = self.scaler.inverse_transform(y)
            pred = self.scaler.inverse_transform(pred)
        
        # plt.plot(x, y.flatten(), label="y_true")
        # plt.plot(x, pred[:, 0], label="y_low")
        # plt.plot(x, pred[:, 1], label="y_mid")
        # plt.plot(x, pred[:, 2], label="y_up")
        
        cal_scores = quantile_err(pred, y)

        # nc = {0: np.sort(cal_scores, 0)[::-1]}
        # significance = .1
        # Sort calibration scores in ascending order? TODO make sure this is correct
        # this is the apply_inverse portion of RegressorNC predict function
        nc = np.sort(cal_scores, 0)#[::-1]
        # print(nc)

        index = int(np.ceil((1 - self.significance) * (nc.shape[0] + 1))) - 1
        # find largest error that gets us guaranteed coverage
        index = min(max(index, 0), nc.shape[0] - 1)

        err_dist = np.vstack([nc[index], nc[index]])

        return err_dist
    
    def calibration_pred(self,x):
        """
        Incorprating the err_dist, predict result
        Args:
            x (torch.Tensor): Input data

        Output: Predicted interval 
        """
        pred = self(x).detach()
        intervals = np.zeros((x.shape[0], 3))
        # ensure that we want to multiply our error distances by the size of our training set
        err_dist = np.hstack([self.err_dist] * x.shape[0])
        # print(self.err_dist)
        # print(err_dist)
        # print(pred)

        intervals[:, 0] = pred[:, 0] - err_dist[0, :]
        intervals[:, 1] = pred[:, 1]
        intervals[:, -1] = pred[:, -1] + err_dist[1, :]
        conformal_intervals = intervals
        return conformal_intervals


    def training_step(self, batch, batch_idx):
        """Trains model for one step.

        Args:
            batch (torch.Tensor): Output of the torch.utils.data.DataLoader
            batch_idx (int): Integer displaying index of this batch
        """
        # print(batch.shape)
        train_loss = self._step(batch, batch_idx, len(self.trainer.train_dataloader))
        self.log(
            "train_loss",
            train_loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return train_loss

    def validation_step(self, batch, batch_idx):
        """Validates model for one step.

        Args:
            batch (torch.Tensor): Output of the torch.utils.data.DataLoader
            batch_idx (int): Integer displaying index of this batch
        """
        
        # do calibration on validation set to prevent overfitting
        if self.method=='conformal':
            self.err_dist = self.calibration(batch, batch_idx, len(self.trainer.val_dataloaders))
        val_loss = self._step(batch, batch_idx, len(self.trainer.val_dataloaders))
        # self.log("val_loss", val_loss)
        return val_loss

    def test_step(self, batch, batch_idx):
        """Tests model for one step.

        Args:
            batch (torch.Tensor): Output of the torch.utils.data.DataLoader
            batch_idx (int): Integer displaying index of this batch
        """
        test_loss = self._step(batch, batch_idx, len(self.trainer.test_dataloaders))
        # self.log("test_loss", test_loss)
        return test_loss

    @abstractmethod
    def forward(self, x, y=None, batches_seen=None):
        """Forward pass.

        Args:
            x (torch.Tensor): Input data

        Returns:
            torch.Tensor: Predicted data
        """

    def predict(self, x):
        """Runs model inference.

        Args:
            x (torch.Tensor): Input data

        Returns:
            torch.Tensor: Predicted data
        """
        return self(x).detach()

    
    def conformal_predict(self, x):
        """Runs model inference.

        Args:
            x (torch.Tensor): Input data

        Returns:
            torch.Tensor: Predicted data
        """
        if self.method == 'conformal':
            val_dataloader = DataLoader(self.val_dataset, batch_size=len(self.val_dataset[0]), shuffle=False)
            self.trainer.validate(self,val_dataloader)
            return self.calibration_pred(x)
        return self(x).detach()

    def configure_optimizers(self):
        """Configure optimizer.

        Returns:
            torch.optim.Optimizer: Optimizer
        """
        optimizer = self.optimizer(self.parameters())

        if self.scheduler is not None:
            scheduler = self.scheduler(optimizer)
            return [optimizer], [scheduler]

        return optimizer


# class TimeSeriesConformalModel(TimeSeriesModel):
#     def __init__(
#         self,
#         optimizer,
#         optimizer_args=None,
#         criterion=F.mse_loss,
#         criterion_args=None,
#         scheduler=None,
#         scheduler_args=None,
#         scaler=None,
#     ):
#         super().__init__()
#         self.criterion = criterion
#         self.criterion_args = criterion_args
#         self.scaler = scaler

#         if optimizer_args is not None:
#             self.optimizer = partial(optimizer, **optimizer_args)
#         else:
#             self.optimizer = optimizer

#         if scheduler is not None and scheduler_args is not None:
#             self.scheduler = partial(scheduler, **scheduler_args)
#         else:
#             self.scheduler = scheduler
    
#     def predict()

#     def quantile_err(prediction, y):
#         """
#         prediction: arr where first 3 columns are: lower quantile, middle quantile (50%), upper quantile in that order
#         """
#         y_lower = prediction[:, 0]
#         y_upper = prediction[:, 2]
#         # Calculate error on our predicted upper and lower quantiles
#         # this will get us an array of negative values with the distance between the upper/lower quantile and the
#         # 50% quantile
#         error_low = y_lower - y
#         error_high = y - y_upper
#         # Make an array where each entry is the highest error when comparing the upper and lower bounds for that entry prediction 
#         err = np.maximum(error_high, error_low)
#         return err