import os
import time
from collections import OrderedDict
from typing import Tuple, List, Dict
import torch
from torchnet import meter
from tensorboardX import SummaryWriter

from common.utils.metrics import precision
from common.utils.fp16 import Fp16Wrap, copy_params, copy_grads
from common.utils.helpers import get_val_from_metric, prepare_checkpoint, \
    save_checkpoint, load_checkpoint


class Callback:
    """
    An abstract class that all callback(e.g., Logger) classes extends from.
    Must be extended before usage.

    usage example:

    mode start (train/infer/debug)
        epoch start (one epoch - one run of every loader)
            loader start
                batch start
                batch handler
                batch end
            loader end
        epoch end
    mode end
    """

    def on_train_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_train_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_infer_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_infer_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_epoch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_epoch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_loader_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_batch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None): pass


class BasicLoggerCallback(Callback):
    """
    Logger callback, translates state.metrics to tensorboard and console output.
    """
    def __init__(
            self,
            loggers: Dict[str, SummaryWriter],
            default_bs: int,
            reset_step: bool = False):
        """
        :param loggers: loggers used during train/infer/debug.
        :param default_bs: default batch size
            for approximate epoch length calucation
        :param reset_step: boolean flag;
            if False - logs will be combine during train/valid
            if True  - logs will be separated
        """
        self.loggers = loggers
        self.default_bs = default_bs
        self.reset_step = reset_step
        self.epoch_metrics = OrderedDict()
        self.time = time.time()

    def on_epoch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        state.epoch_metrics = OrderedDict()
        self.epoch_metrics = OrderedDict()

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode
        self.time = time.time()
        state.step = (
                state.step
                or state.epoch * len(state.loader) * self.default_bs)
        state.epoch_metrics[lm] = {}
        self.epoch_metrics[lm] = {}

        self.epoch_metrics[lm]["batch time"] = meter.AverageValueMeter()
        self.epoch_metrics[lm]["sample per second"] = meter.AverageValueMeter()
        self.epoch_metrics[lm]["loss"] = meter.AverageValueMeter()
        for key in optimizer:
            self.epoch_metrics[lm][f"lr_{key}"] = meter.AverageValueMeter()
            self.epoch_metrics[lm][f"momentum_{key}"] = \
                meter.AverageValueMeter()

    def on_batch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.loggers[state.loader_mode].add_scalar(
            "data time", time.time() - self.time, state.step)

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode
        state.bs = state.bs or state.target.shape[0]
        elapsed_time = time.time() - self.time

        self.epoch_metrics[lm]["batch time"].add(elapsed_time)
        self.loggers[lm].add_scalar("batch time", elapsed_time, state.step)
        self.epoch_metrics[lm]["sample per second"].add(state.bs / elapsed_time)
        self.loggers[lm].add_scalar(
            "sample per second", state.bs / elapsed_time, state.step)
        for key, value in state.lr.items():
            self.epoch_metrics[lm][f"lr_{key}"].add(value)
            self.loggers[lm].add_scalar(f"lr_{key}", value, state.step)
        for key, value in state.momentum.items():
            self.epoch_metrics[lm][f"momentum_{key}"].add(value)
            self.loggers[lm].add_scalar(f"momentum_{key}", value, state.step)

        loss_ = state.loss.item()
        self.epoch_metrics[lm]["loss"].add(loss_)
        self.loggers[lm].add_scalar("loss", loss_, state.step)

        self.time = time.time()
        state.step += state.bs

    def on_loader_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode

        state.epoch_metrics[lm] = {
            **state.epoch_metrics[lm],
            **self.epoch_metrics[lm]
        }

        state.epoch_metrics[lm] = {
            key: get_val_from_metric(value)
            for key, value in state.epoch_metrics[lm].items()}

        for key, value in state.epoch_metrics[lm].items():
            self.loggers[lm].add_scalar("epoch " + key, value, state.epoch)

        epoch_metrics_str = "\t".join([
            "{key} {value:.4f}".format(key=key, value=value)
            for key, value in sorted(state.epoch_metrics[lm].items())])

        print("{epoch} * Epoch ({mode}): {metrics}".format(
            epoch=state.epoch, mode=lm, metrics=epoch_metrics_str))

        if self.reset_step:
            state.step = None


class PrecisionCallback(Callback):
    """
    Precision metric callback.
    """
    def __init__(
            self,
            loggers: Dict[str, SummaryWriter],
            input_key: str,
            output_key: str,
            precision_args: List[int] = None):
        """
        @TODO: make it loggers agnostic - all logs through LoggerCallback
        :param loggers: loggers used during train/infer/debug.
        :param input_key: input key to use for precision calculation;
            specifies our `y_true`.
        :param output_key: output key to use for precision calculation;
            specifies our `y_pred`.
        :param precision_args: specifies which precision@K to log.
            [1] - accuracy
            [1, 3] - accuracy and precision@3
            [1, 3, 5] - precision at 1, 3 and 5
        """
        super().__init__()
        self.loggers = loggers
        self.input_key = input_key
        self.output_key = output_key
        self.precision_args = precision_args or [1, 3, 5]
        self.epoch_metrics = OrderedDict()

    def on_epoch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.epoch_metrics = OrderedDict()

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode
        self.epoch_metrics[lm] = OrderedDict()
        for p in self.precision_args:
            self.epoch_metrics[lm]["precision{:02}".format(p)] = \
                meter.AverageValueMeter()

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode

        prec = precision(
            state.output[self.output_key],
            state.input[self.input_key],
            topk=self.precision_args)

        for p, metric in zip(self.precision_args, prec):
            key = "precision{:02}".format(p)
            metric_ = metric.item()
            self.epoch_metrics[lm][key].add(metric_)
            self.loggers[lm].add_scalar(key, metric_, state.step)

    def on_loader_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        lm = state.loader_mode
        state.epoch_metrics[lm] = {
            **state.epoch_metrics[lm],
            **self.epoch_metrics[lm]
        }


class CheckpointCallback(Callback):
    """
    Checkpoint callback to save/restore your mode/criterion/optimizer/metrics.
    """
    def __init__(
            self,
            logdir: str = None,
            save_n_best: int = 5,
            resume: str = None,
            main_metric: str = "loss",
            minimize: bool = True):
        """
        :param logdir: log directory to use for checkpoint saving
        :param save_n_best: number of best checkpoiont to keep
        :param resume: path to checkpoint to load and initialize runner state
        :param main_metric: which metric to use for checkpoint comparison
        :param minimize: boolean flag if we need to minimize or maximize metric
        """
        self.logdir = logdir
        self.save_n_best = save_n_best
        self.resume = resume
        self.main_metric = main_metric
        self.minimize = minimize
        self.top_best_metrics = []

    @staticmethod
    def load_checkpoint(
            *, filename, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if os.path.isfile(filename):
            print("=> loading checkpoint \"{}\"".format(filename))
            checkpoint = load_checkpoint(filename)

            state.epoch = checkpoint["epoch"]
            state.best_metrics = checkpoint["best_metrics"]

            if model is not None:
                if isinstance(model, torch.nn.DataParallel):
                    model = model.module
                if isinstance(model, Fp16Wrap):
                    model.network.load_state_dict(
                        checkpoint["model_state_dict"])
                else:
                    model.load_state_dict(
                        checkpoint["model_state_dict"])

            if optimizer is not None:
                for key in optimizer:
                    optimizer[key].load_state_dict(
                        checkpoint["optimizer_" + str(key) + "_state_dict"])

            if scheduler is not None:
                for key in scheduler:
                    scheduler[key] = checkpoint["scheduler_" + str(key)]

            print("loaded checkpoint \"{}\" (epoch {})"
                  .format(filename, checkpoint["epoch"]))
        else:
            raise Exception("no checkpoint found at \"{}\"".format(filename))

    def save_checkpoint(self, logdir, checkpoint, is_best, save_n_best=5):
        filepath = save_checkpoint(
            logdir=logdir, checkpoint=checkpoint,
            is_best=is_best,
            suffix=str(checkpoint.get("epoch", "")))
        self.top_best_metrics.append((
            filepath, checkpoint["valid_metrics"][self.main_metric]))
        self.top_best_metrics = sorted(
            self.top_best_metrics, key=lambda x: x[1],
            reverse=not self.minimize)
        if len(self.top_best_metrics) > save_n_best:
            last_item = self.top_best_metrics.pop(-1)
            last_filepath = last_item[0]
            os.remove(last_filepath)

    def prepare_checkpoint(self, **kwargs):
        return prepare_checkpoint(**kwargs)

    @staticmethod
    def process_epoch_metrics(
            epoch_metrics, best_metrics,
            main_metric="loss", minimize=True):
        valid_metrics = None
        for key, value in epoch_metrics.items():
            if key.startswith("valid"):
                valid_metrics = value
        is_best = True \
            if best_metrics is None \
            else (minimize != (
                valid_metrics[main_metric] > best_metrics[main_metric]))
        best_metrics = valid_metrics if is_best else best_metrics
        return best_metrics, valid_metrics, is_best

    def on_mode_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if self.resume is not None:
            self.load_checkpoint(
                filename=self.resume, state=state, model=model,
                criterion=criterion, optimizer=optimizer, scheduler=scheduler)

    def on_train_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        return self.on_mode_start(
            state=state, model=model,
            criterion=criterion, optimizer=optimizer, scheduler=scheduler)

    def on_infer_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        return self.on_mode_start(
            state=state, model=model,
            criterion=criterion, optimizer=optimizer, scheduler=scheduler)

    def on_epoch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if not state.loader_mode.startswith("valid"):
            return

        best_metrics, valid_metrics, is_best = self.process_epoch_metrics(
            state.epoch_metrics, state.best_metrics,
            main_metric=self.main_metric, minimize=self.minimize)
        valid_metrics = {
            key: value
            for key, value in valid_metrics.items()
            if isinstance(value, float)
        }
        state.best_metrics = {
            key: value
            for key, value in best_metrics.items()
            if isinstance(value, float)
        }

        checkpoint = self.prepare_checkpoint(
            model=model,
            criterion=criterion,
            optimizer=optimizer,
            scheduler=scheduler,
            valid_metrics=valid_metrics,
            epoch_metrics=state.epoch_metrics,
            best_metrics=state.best_metrics,
            epoch=state.epoch)
        self.save_checkpoint(
            logdir=self.logdir,
            checkpoint=checkpoint,
            is_best=is_best,
            save_n_best=self.save_n_best)

    def on_train_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        print("Top best models:")
        top_best_metrics_str = "\n".join([
            "{filepath}\t{metric:.4f}".format(filepath=filepath, metric=metric)
            for filepath, metric in self.top_best_metrics])
        print(top_best_metrics_str)


class BasicOptimizerCallback(Callback):
    """
    Optimizer callback, abstraction over optimizer step.
    """
    def __init__(
            self,
            grad_clip: float = None,
            fp16_grad_scale : float = 128.0):
        """
        :param grad_clip: grap clipping specification kwargs
            @TODO: better support of different grad clip funcs
        :param fp16_grad_scale: grad scale for fp16 mode training
        """
        self.optimizer_wds = {}
        self.grad_clip = grad_clip
        self.fp16 = False
        self.fp16_grad_scale = fp16_grad_scale

    def on_train_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.fp16 = isinstance(model, Fp16Wrap)

    def on_epoch_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.optimizer_wds = {}
        for key, optimizer_ in optimizer.items():
            wd = optimizer_.param_groups[0].get("weight_decay", 0.0)
            if wd > 0:
                self.optimizer_wds[key] = wd
                optimizer_.param_groups[0]["weight_decay"] = 0.0

    def grad_step(self, optimizer):
        for key, value in optimizer.items():
            if key in self.optimizer_wds:
                wd = self.optimizer_wds[key]
                for group in value.param_groups:
                    for param in group["params"]:
                        param.data = param.data.add(
                            -wd * group["lr"], param.data)
                    if self.grad_clip is not None:
                        torch.nn.utils.clip_grad_norm_(
                            group["params"], self.grad_clip)
            value.step()

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if not state.is_train:
            return

        if not self.fp16:
            for _, value in optimizer.items():
                value.zero_grad()

            if len(optimizer) > 0:
                state.loss.backward()
                self.grad_step(optimizer)
        else:
            model.zero_grad()
            if len(optimizer) > 0:
                assert len(optimizer) == 1, \
                    "fp16 mode works only with one optimizer for now"
                scaled_loss = self.fp16_grad_scale * state.loss.float()
                scaled_loss.backward()

                master_params = list(
                    optimizer["main"].param_groups[0]["params"])
                model_params = list(filter(
                    lambda p: p.requires_grad, model.parameters()))

                copy_grads(source=model_params, target=master_params)

                for param in master_params:
                    param.grad.data.mul_(1. / self.fp16_grad_scale)

                self.grad_step(optimizer)

                copy_params(source=master_params, target=model_params)
                torch.cuda.synchronize()

    def on_epoch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        for key, value in self.optimizer_wds.items():
            optimizer[key].param_groups[0]["weight_decay"] = value


class LRUpdater(Callback):
    """Basic class that all Lr updaters inherit from"""
    def __init__(
            self,
            init_lr: float,
            optimizer_key: str = "main"):
        """
        :param init_lr: initial learning rate to use
            @TODO: pick it automatically from optimizer
        :param optimizer_key: which optimizer key to use
            for learning rate scheduling
        """
        self.init_lr = init_lr
        self.optimizer_key = optimizer_key

    def calc_lr(self):
        return None

    def calc_momentum(self):
        return None

    @staticmethod
    def update_lr(optimizer, new_lr):
        for pg in optimizer.param_groups:
            pg["lr"] = new_lr

    @staticmethod
    def update_momentum(optimizer, new_momentum):
        if "betas" in optimizer.param_groups[0]:
            for pg in optimizer.param_groups:
                pg["betas"] = (new_momentum, pg["betas"][1])
        else:
            for pg in optimizer.param_groups:
                pg["momentum"] = new_momentum

    def update_optimizer(self, state, optimizer):
        if state.is_train:
            new_lr = self.calc_lr()
            if new_lr is not None:
                self.update_lr(
                    optimizer[self.optimizer_key], new_lr)
                state.lr[self.optimizer_key] = new_lr
            new_momentum = self.calc_momentum()
            if new_momentum is not None:
                self.update_momentum(
                    optimizer[self.optimizer_key], new_momentum)
                state.momentum[self.optimizer_key] = new_momentum
        else:
            state.lr[self.optimizer_key] = 0
            state.momentum[self.optimizer_key] = 0

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.update_optimizer(state=state, optimizer=optimizer)

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        self.update_optimizer(state=state, optimizer=optimizer)


class OneCycleLR(LRUpdater):
    """
    An learning rate updater
        that implements the Circular Learning Rate (CLR) scheme.
    Learning rate is increased then decreased linearly.
    """
    def __init__(
            self,
            init_lr: float,
            cycle_len: int,
            div: int,
            cut_div: int,
            momentum_range: Tuple[float, float],
            optimizer_key: str = "main"):
        """

        :param init_lr: init learning rate for torch optimizer
        :param cycle_len: (int) num epochs to apply one cycle policy
        :param div: (int) ratio between initial lr and maximum lr
        :param cut_div: (int) which part of cycle lr will grow
            (Ex: cut_div=4 -> 1/4 lr grow, 3/4 lr decrease
        :param momentum_range: (tuple(int, int)) max and min momentum values
        :param optimizer_key: which optimizer key to use
            for learning rate scheduling
        """
        super().__init__(init_lr=init_lr, optimizer_key=optimizer_key)
        self.total_iter = None
        self.div = div
        self.cut_div = cut_div
        self.cycle_iter = 0
        self.cycle_count = 0
        self.cycle_len = cycle_len
        # point in iterations for starting lr decreasing
        self.cut_point = None
        self.momentum_range = momentum_range

    def calc_lr(self):
        # calculate percent for learning rate change
        if self.cycle_iter > self.cut_point:
            percent = (
                    1 - (self.cycle_iter - self.cut_point) /
                    (self.total_iter - self.cut_point))
        else:
            percent = self.cycle_iter / self.cut_point
        res = self.init_lr * (1 + percent * (self.div - 1)) / self.div

        self.cycle_iter += 1
        if self.cycle_iter == self.total_iter:
            self.cycle_iter = 0
            self.cycle_count += 1
        return res

    def calc_momentum(self):
        if self.cycle_iter > self.cut_point:
            now_ = (self.cycle_iter - self.cut_point)
            all_ = (self.total_iter - self.cut_point)
            percent = now_ / all_
        else:
            percent = 1 - self.cycle_iter / self.cut_point
        res = self.momentum_range[1] \
              + percent * (self.momentum_range[0] - self.momentum_range[1])
        return res

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if state.is_train:
            self.total_iter = len(state.loader) * self.cycle_len
            self.cut_point = self.total_iter // self.cut_div

        super().on_loader_start(
            state=state, model=model, criterion=criterion,
            optimizer=optimizer, scheduler=scheduler)


class LRFinder(LRUpdater):
    """
    Helps you find an optimal learning rate for a model,
        as per suggetion of 2015 CLR paper.
    Learning rate is increased in linear or log scale, depending on user input.

    https://sgugger.github.io/how-do-you-find-a-good-learning-rate.html
    """
    def __init__(
            self,
            init_lr,
            final_lr,
            n_steps=None,
            optimizer_key="main"):
        """

        :param init_lr: initial learning rate to use
        :param final_lr: final learning rate to try with
        :param n_steps:  number of batches to try;
            if None - whole loader would be used.
        :param optimizer_key: which optimizer key to use
            for learning rate scheduling
        """
        super().__init__(init_lr, optimizer_key=optimizer_key)

        self.final_lr = final_lr
        self.init_lr = init_lr
        self.n_steps = n_steps
        self.multiplier = 0
        self.find_iter = 0

    def calc_lr(self):
        res = self.init_lr * self.multiplier ** self.find_iter
        self.find_iter += 1
        return res

    def on_batch_end(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        super().on_batch_end(
            state=state, model=model, criterion=criterion,
            optimizer=optimizer, scheduler=scheduler)
        if self.find_iter > self.n_steps:
            raise NotImplementedError("End of LRFinder")

    def on_loader_start(
            self, *, state,
            model=None, criterion=None, optimizer=None, scheduler=None):
        if state.is_train:
            lr_ = self.final_lr / self.init_lr
            self.n_steps = self.n_steps or len(state.loader)
            self.multiplier = lr_ ** (1 / self.n_steps)

        super().on_loader_start(
            state=state, model=model, criterion=criterion,
            optimizer=optimizer, scheduler=scheduler)