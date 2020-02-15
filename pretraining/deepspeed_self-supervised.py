import argparse
import deepspeed

import re
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, random_split

from tqdm import tqdm

from reformer_pytorch import ReformerLM
from transformers import BertTokenizer, PreTrainedTokenizer
from fairseq.optim.adafactor import Adafactor
import os
import json
import logging
from datetime import datetime


class WikiDataset(Dataset):

    def __init__(self, path="", prefix="train"):

        assert os.path.isdir(path)

        self.documents = []
        filename_list = os.listdir(path)
        for file in filename_list:
            path_to_file = os.path.join(path, file)
            if not os.path.isfile(path_to_file):
                continue
            self.documents.append(path_to_file)

    def __len__(self):
        """ Returns the number of documents. """
        return len(self.documents)

    def __getitem__(self, idx):
        document_path = self.documents[idx]
        document_name = document_path.split("/")[-1]

        items = []

        with open(document_path, encoding="utf-8") as source:
            raw_text = source.readlines()
            for obj in raw_text:
                text = json.loads(obj)['text']
                text = re.sub('\\n', ' ', text)
                text = re.sub('\\s+', ' ', text)
                items.append(text)

        return items


class ReformerTrainer(object):

    def __init__(self,
                 dataset,
                 model,
                 tokenizer,
                 device=None,
                 train_batch_size=8,
                 eval_batch_size=None,
                 tb_writer=True,
                 tb_dir='./tb_logs',
                 log_dir='./logs'):
        """
        Provides an easy to use class for pretraining and evaluating a Reformer Model.

        :param dataset: (torch.utils.data.Dataset) containing all of the data you wish to utilize during training.
        :param model: (reformer_pytorch.Reformer)
        :param tokenizer: (transformers.PreTrainedTokenizer) defaults to BertTokenizer ('bert-base-case')
        :param device: provide manual device placement. If None, will default to cuda:0 if available.
        :param tb_writer: (bool) Whether to write to tensorboard or not.
        :param tb_dir: (str) Where to write TB logs to.
        :param log_dir: (str) Where to write generic logs to.
        """

        self.dataset = dataset
        self.model = model
        self.tokenizer = tokenizer
        self.device = device
        self.n_gpu = torch.cuda.device_count() if torch.cuda.is_available() else 0
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size
        self.tb_writer = tb_writer
        self.log_dir = log_dir

        if tokenizer is None:
            self.tokenizer = BertTokenizer.from_pretrained('bert-base-cased')

        if device is None:
            self.device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

        if eval_batch_size is None:
            self.eval_batch_size = train_batch_size

        if tb_writer:
            from torch.utils.tensorboard import SummaryWriter
            self.writer = SummaryWriter(log_dir=tb_dir)

        logging.basicConfig(filename=f'{log_dir}/{datetime.now().date()}.log', level=logging.INFO)

    def split_datasets(self, train_test_split=0.1):
        """
        Splits self.dataset into a train and test dataset.
        :param train_test_split: ratio of test to train data.
        :return: 2 torch.utils.data.Dataset
        """
        dataset_len = len(self.dataset)
        eval_len = int(dataset_len * train_test_split)
        train_len = dataset_len - eval_len
        train_dataset, eval_dataset = random_split(self.dataset, (train_len, eval_len))
        return train_dataset, eval_dataset

    def build_dataloaders(self, train_dataset, eval_dataset, train_shuffle=True, eval_shuffle=True):
        """
        Builds the Training and Eval DataLoaders

        :param train_dataset: torch.utils.data.Dataset of Training Data.
        :param eval_dataset: torch.utils.data.Dataset of Evaluation Data.
        :param train_shuffle: (bool) True if you wish to shuffle the train_dataset.
        :param eval_shuffle: (bool) True if you wish to shuffle the eval_dataset.
        :return: train dataloader and evaluation dataloader.
        """

        train_loader = DataLoader(train_dataset, batch_size=self.train_batch_size, shuffle=train_shuffle)
        eval_loader = DataLoader(eval_dataset, batch_size=self.eval_batch_size, shuffle=eval_shuffle)
        logging.info(f'''train_dataloader size: {len(train_loader.dataset)} | shuffle: {train_shuffle}
                         eval_dataloader size: {len(eval_loader.dataset)} | shuffle: {eval_shuffle}''')
        return train_loader, eval_loader

    def train(self,
              epochs,
              train_dataloader,
              eval_dataloader,
              log_steps,
              ckpt_steps,
              ckpt_dir=None,
              gradient_accumulation_steps=1):
        """
        Trains the Reformer Model
        :param epochs: The number of times you wish to loop through the dataset.
        :param train_dataloader: (torch.utils.data.DataLoader) The data to train on.
        :param eval_dataloader: (torch.utils.data.DataLoader) The data to evaluate on.
        :param log_steps: The number of steps to iterate before logging.
        :param ckpt_steps: The number of steps to iterate before checkpointing.
        :param ckpt_dir: The directory to save the checkpoints to.
        :param gradient_accumulation_steps: Optional gradient accumulation.
        :return: Total number of steps, total loss, model
        """

        optimizer = Adafactor(self.model.parameters())
        loss_fn = nn.CrossEntropyLoss()
        losses = {}
        global_steps = 0
        local_steps = 0
        step_loss = 0.0

        if ckpt_dir is not None:
            assert os.path.isdir(ckpt_dir)
            try:
                logging.info(f'{datetime.now()} | Continuing from checkpoint...')
                self.model.load_state_dict(torch.load(f'{ckpt_dir}/model_state_dict.pt', map_location=self.device))
                optimizer.load_state_dict(torch.load(f'{ckpt_dir}/optimizer_state_dict.pt'))

            except Exception as e:
                logging.info(f'{datetime.now()} | No checkpoint was found | {e}')

        self.model.train()

        if self.n_gpu > 1:
            self.model = nn.DataParallel(self.model)
            logging.info(f'{datetime.now()} | Utilizing {self.n_gpu} GPUs')

        self.model.to(self.device)
        logging.info(f'{datetime.now()} | Moved model to: {self.device}')
        logging.info(
            f'{datetime.now()} | train_batch_size: {self.train_batch_size} | eval_batch_size: {self.eval_batch_size}')
        logging.info(f'{datetime.now()} | Epochs: {epochs} | log_steps: {log_steps} | ckpt_steps: {ckpt_steps}')
        logging.info(f'{datetime.now()} | gradient_accumulation_steps: {gradient_accumulation_steps}')

        for epoch in tqdm(range(epochs), desc='Epochs', position=0):
            logging.info(f'{datetime.now()} | Epoch: {epoch}')
            for step, batch in tqdm(enumerate(train_dataloader),
                                    desc='Epoch Iterator',
                                    position=1,
                                    leave=True,
                                    total=len(train_dataloader)):
                for data in batch:
                    inputs = self._tokenize_input_ids(data, pad_to_max_length=True)
                    inputs, labels = self.mask_tokens(inputs)
                    inputs, labels = inputs.to(self.device), labels.to(self.device)
                    output = self.model(inputs)

                    # only calculating loss on masked tokens
                    loss_mx = labels != -100
                    output = output[loss_mx].view(-1, self.tokenizer.vocab_size)
                    labels = labels[loss_mx].view(-1)

                    loss = loss_fn(output, labels)

                    if gradient_accumulation_steps > 1:
                        loss /= gradient_accumulation_steps

                    loss.backward()
                    optimizer.step()
                    self.model.zero_grad()

                    step_loss += loss.item()
                    losses[global_steps] = loss.item()
                    local_steps += 1
                    global_steps += 1

                    if global_steps % log_steps == 0:
                        if self.tb_writer:
                            self.writer.add_scalar('Train/Loss', step_loss / local_steps, global_steps)
                            self.writer.close()
                        logging.info(
                            f'''{datetime.now()} | Train Loss: {step_loss / local_steps} | Steps: {global_steps}''')

                        with open(f'{self.log_dir}/train_results.json', 'w') as results_file:
                            json.dump(losses, results_file)
                            results_file.close()
                        step_loss = 0.0
                        local_steps = 0

                    if global_steps % ckpt_steps == 0:
                        # evaluating before every checkpoint
                        self.evaluate(eval_dataloader)
                        model_to_save = self.model.module if hasattr(self.model, 'module') else self.model
                        torch.save(model_to_save.state_dict(), f'{ckpt_dir}/model_state_dict.pt')
                        torch.save(optimizer.state_dict(), f'{ckpt_dir}/optimizer_state_dict.pt')

                        logging.info(f'{datetime.now()} | Saved checkpoint to: {ckpt_dir}')

        model_to_save = self.model.module if hasattr(self.model, 'module') else self.model
        torch.save(model_to_save.state_dict(), f'{ckpt_dir}/model_state_dict.pt')
        torch.save(optimizer.state_dict(), f'{ckpt_dir}/optimizer_state_dict.pt')

        return self.model

    def evaluate(self, dataloader):
        """
        Runs through the provided dataloader with torch.no_grad()
        :param dataloader: (torch.utils.data.DataLoader) Evaluation DataLoader
        :return: None
        """
        loss_fn = nn.CrossEntropyLoss()

        if self.n_gpu > 1 and not isinstance(self.model, nn.DataParallel):
            self.model = nn.DataParallel(self.model)

        self.model.eval()
        eval_loss = 0.0
        perplexity = 0.0
        eval_steps = 0

        logging.info(f'{datetime.now()} | Evaluating...')
        for step, batch in tqdm(enumerate(dataloader), desc='Evaluating', leave=True, total=len(dataloader)):
            for data in batch:
                inputs = self._tokenize_input_ids(data, pad_to_max_length=True)
                inputs, labels = self.mask_tokens(inputs)
                inputs, labels = inputs.to(self.device), labels.to(self.device)

                with torch.no_grad():
                    output = self.model(inputs)

                loss_mx = labels != -100
                output_ids = output[loss_mx].view(-1, self.tokenizer.vocab_size)
                labels = labels[loss_mx].view(-1)
                tmp_eval_loss = loss_fn(output_ids, labels)
                tmp_perplexity = torch.exp(tmp_eval_loss)

                if self.n_gpu > 1:
                    tmp_eval_loss = tmp_eval_loss.mean()

                eval_loss += tmp_eval_loss.item()
                perplexity += tmp_perplexity.item()
                eval_steps += 1

            eval_loss /= eval_steps
            perplexity /= eval_steps

            if self.tb_writer:
                self.writer.add_scalar('Eval/Loss', eval_loss, eval_steps)
                self.writer.close()
                self.writer.add_scalar('Perplexity', perplexity, eval_steps)
                self.writer.close()
            logging.info(f'{datetime.now()} | Step: {step} | Eval Loss: {eval_loss} | Perplexity: {perplexity}')

        return None


if __name__ == '__main__':
    dataset = WikiDataset(path='./data/enwiki')
    tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
    tokenizer.max_len = 128
    model = ReformerLM(
        num_tokens=tokenizer.vocab_size,
        dim=512,
        depth=6,
        heads=8,
        max_seq_len=tokenizer.max_len,
        causal=True
    )

    parameters = filter(lambda p: p.requires_grad, model.parameters())

    parser = argparse.ArgumentParser(description='Reformer')

    # data
    # cuda
    parser.add_argument('--with_cuda', default=False, action='store_true', dest='with_cuda',
                        help='use CPU in case there\'s no GPU support')
    parser.add_argument('--use_ema', default=False, action='store_true', dest='use_ema',
                        help='whether use exponential moving average')

    # train
    parser.add_argument('-b', '--batch_size', default=32, type=int, dest='batch_size',
                        help='mini-batch size (default: 32)')
    parser.add_argument('-e', '--epochs', default=3, type=int, dest='epochs',
                        help='number of total epochs (default: 3)')
    parser.add_argument('--local_rank', type=int, default=-1, dest='local_rank',
                        help='local rank passed from distributed launcher')
    parser.add_argument('--tb_writer', default=False, action='store_true', dest='tb_writer',
                        help='Whether to write tensorboard logs or not')
    parser.add_argument('--ckpt_dir', default='./ckpt_dir', type=str, dest='ckpt_dir',
                        help='directory to save and load checkpoints to')
    parser.add_argument('--log_dir', default='./ckpt_dir', type=str, dest='ckpt_dir',
                        help='directory to save and load checkpoints to')
    parser.add_argument('--ckpt_id', type=int, dest='ckpt_id',
                        help='The ckpt you wish to continue from.')

    # Include DeepSpeed configuration arguments
    parser = deepspeed.add_config_arguments(parser)

    args = parser.parse_args()

    if args.with_cuda:
        assert torch.cuda.is_available()

    trainer = ReformerTrainer(
        dataset,
        model,
        tokenizer,
        train_batch_size=args.batch_size,
        eval_batch_size=args.batch_size,
        tb_writer=False
    )
    train_dataset, eval_dataset = trainer.split_datasets(train_test_split=0.1)
    train_dataloader, eval_dataloader = trainer.build_dataloaders(train_dataset, eval_dataset)

    model_engine, optimizer, trainloader, __ = deepspeed.initialize(
        args=args,
        model=model,
        model_parameters=parameters,
        training_data=train_dataset
    )


    def mask_tokens(tokenizer, inputs: torch.Tensor, mlm_probability=0.15, pad=True):
        """ Prepare masked tokens inputs/labels for masked language modeling: 80% MASK, 10% random, 10% original. """
        labels = inputs.clone()
        # mlm_probability defaults to 0.15 in Bert
        probability_matrix = torch.full(labels.shape, mlm_probability)
        special_tokens_mask = [
            tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in labels.tolist()
        ]
        probability_matrix.masked_fill_(torch.tensor(special_tokens_mask, dtype=torch.bool), value=0.0)
        if tokenizer._pad_token is not None:
            padding_mask = labels.eq(self.tokenizer.pad_token_id)
            probability_matrix.masked_fill_(padding_mask, value=0.0)
        masked_indices = torch.bernoulli(probability_matrix).bool()
        labels[~masked_indices] = -100  # We only compute loss on masked tokens

        # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
        indices_replaced = torch.bernoulli(torch.full(labels.shape, 0.8)).bool() & masked_indices
        inputs[indices_replaced] = tokenizer.convert_tokens_to_ids(self.tokenizer.mask_token)

        # 10% of the time, we replace masked input tokens with random word
        indices_random = torch.bernoulli(torch.full(labels.shape, 0.5)).bool() & masked_indices & ~indices_replaced
        random_words = torch.randint(len(tokenizer), labels.shape, dtype=torch.long)
        inputs[indices_random] = random_words[indices_random]

        if pad:
            input_pads = tokenizer.max_len - inputs.shape[-1]
            label_pads = tokenizer.max_len - labels.shape[-1]

            inputs = F.pad(inputs, pad=(0, input_pads), value=tokenizer.pad_token_id)
            labels = F.pad(labels, pad=(0, label_pads), value=tokenizer.pad_token_id)

        # The rest of the time (10% of the time) we keep the masked input tokens unchanged
        return inputs, labels


    def _tokenize_input_ids(tokenizer, input_ids: list, pad_to_max_length: bool = True):
        """
        Helper function to clean up the train and eval functions
        :param input_ids: inputs to tokenize.
        :param pad_to_max_length: Whether you want to pad the inputs to the tokenizer.max_len
        :return: Tensor containing training data.
        """
        inputs = torch.cat(
            [
                tokenizer.encode(
                    input_ids[i],
                    add_special_tokens=True,
                    max_length=tokenizer.max_len,
                    pad_to_max_length=pad_to_max_length,
                    return_tensors='pt'
                ) \
                for i in range(len(input_ids))
            ]
        )
        return inputs


    def train(args,
              model,
              optimizer,
              tokenizer,
              train_dataloader,
              log_steps,
              ckpt_steps,
              gradient_accumulation_steps=1):
        """
        Trains the Reformer Model
        :param args: deepspeed args.
        :param model: Deepspeed enabled model_engine.
        :param optimizer: Deepspeed enabled optimizer.
        :param tokenizer: BertTokenizer to tokenize textual inputs.
        :param train_dataloader: (torch.utils.data.DataLoader) The data to train on.
        :param log_steps: The number of steps to iterate before logging.
        :param ckpt_steps: The number of steps to iterate before checkpointing.
        :param gradient_accumulation_steps: Optional gradient accumulation.
        :return: Total number of steps, total loss, model.
        """

        loss_fn = nn.CrossEntropyLoss()
        losses = {}
        global_steps = 0
        local_steps = 0
        step_loss = 0.0

        if args.ckpt_dir is not None and args.ckpt_id is not None:
            assert os.path.isdir(args.ckpt_dir)
            assert os.path.isfile(f'{args.ckpt_dir}/{args.ckpt_id}')
            try:
                logging.info(f'{datetime.now()} | Continuing from checkpoint...')
                _, client_sd = model_engine.load_checkpoint(args.ckpt_dir, args.ckpt_id)

            except Exception as e:
                logging.info(f'{datetime.now()} | No checkpoint was found | {e}')

        logging.info(f'''{datetime.now()}
batch_size: {args.batch_size}
epochs: {args.epochs}
log_steps: {log_steps}
ckpt_steps: {ckpt_steps}
gradient_accumulation_steps: {gradient_accumulation_steps}
''')

        for epoch in tqdm(range(args.epochs), desc='Epochs', position=0):
            logging.info(f'{datetime.now()} | Epoch: {epoch}')
            for step, batch in tqdm(enumerate(train_dataloader),
                                    desc='Epoch Iterator',
                                    position=1,
                                    leave=True,
                                    total=len(train_dataloader)):
                for data in batch:
                    inputs = _tokenize_input_ids(tokenizer, data, pad_to_max_length=True)
                    inputs, labels = mask_tokens(tokenizer, inputs)
                    inputs, labels = inputs.to(model.local_rank), labels.to(model.local_rank)

                    output = model(inputs)

                    # only calculating loss on masked tokens
                    loss_mx = labels != -100
                    output = output[loss_mx].view(-1, tokenizer.vocab_size)
                    labels = labels[loss_mx].view(-1)

                    loss = loss_fn(output, labels)

                    if gradient_accumulation_steps > 1:
                        loss /= gradient_accumulation_steps

                    step_loss += loss.item()
                    losses[global_steps] = loss.item()
                    local_steps += 1
                    global_steps += 1

                    model.backward(loss)
                    model.step()
                    # gradient zero-ing is implemented automatically by deepspeed

                    if global_steps % log_steps == 0:
                        logging.info(
                            f'''{datetime.now()} | Train Loss: {step_loss / local_steps} | Steps: {global_steps}''')

                        with open(f'{args.log_dir}/train_results.json', 'w') as results_file:
                            json.dump(losses, results_file)
                            results_file.close()
                        step_loss = 0.0
                        local_steps = 0

                    if global_steps % ckpt_steps == 0:
                        ckpt_id = loss.item()
                        model.save_checkpoint(args.ckpt_dir, ckpt_id)
                        # evaluating before every checkpoint
                        evaluate(model, eval_dataloader)
                        logging.info(f'{datetime.now()} | Saved checkpoint to: {args.ckpt_dir}')
        logging.info(f'{datetime.now()} | Finished Training | model_type: {type(model)}')
        torch.save(model, f'{args.ckpt_dir}/model.bin')
        return model


    def evaluate(model, dataloader):
        """
        Runs through the provided dataloader with torch.no_grad()
        :param model: Deepspeed model
        :param dataloader: (torch.utils.data.DataLoader) Evaluation DataLoader
        :return: None
        """
        loss_fn = nn.CrossEntropyLoss()

        eval_loss = 0.0
        perplexity = 0.0
        eval_steps = 0

        logging.info(f'{datetime.now()} | Evaluating...')
        for step, batch in tqdm(enumerate(dataloader), desc='Evaluating', leave=True, total=len(dataloader)):
            for data in batch:
                inputs = _tokenize_input_ids(tokenizer, data, pad_to_max_length=True)
                inputs, labels = mask_tokens(tokenizer, inputs)
                inputs, labels = inputs.to(model.local_rank), labels.to(model.local_rank)

                with torch.no_grad():
                    output = model(inputs)

                loss_mx = labels != -100
                output_ids = output[loss_mx].view(-1, tokenizer.vocab_size)
                labels = labels[loss_mx].view(-1)
                tmp_eval_loss = loss_fn(output_ids, labels)
                tmp_perplexity = torch.exp(tmp_eval_loss)

                eval_loss += tmp_eval_loss.item()
                perplexity += tmp_perplexity.item()
                eval_steps += 1

            eval_loss /= eval_steps
            perplexity /= eval_steps

            logging.info(f'{datetime.now()} | Step: {step} | Eval Loss: {eval_loss} | Perplexity: {perplexity}')

        return None
