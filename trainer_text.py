# import
import argparse
import emoji
import re
import torch
import random
import os
import pickle
import numpy as np
import multiprocessing
import pandas as pd
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from transformers import BertTokenizer, BertModel
from Sastrawi.StopWordRemover.StopWordRemoverFactory import StopWordRemoverFactory
from tqdm import tqdm
from Sastrawi.Stemmer.StemmerFactory import StemmerFactory
from torch.utils.data import TensorDataset
from collections import defaultdict


# setup
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
pd.options.display.float_format = '{:,.2f}'.format  

parser = argparse.ArgumentParser()
parser.add_argument("--data", type=str, default="abstrak")
parser.add_argument("--dataset", type=str, default='data_skripsi_jtik.csv')
parser.add_argument("--batch_size", type=int, default=32)
parser.add_argument("--bert_model", type=str, default="indolem/indobertweet-base-uncased")
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--max_epochs", type=int, default=30)
parser.add_argument("--lr", type=float, default=2e-5)
parser.add_argument("--dropout", type=float, default=0.1)
parser.add_argument("--patience", type=int, default=3)
config = vars(parser.parse_args())

np.random.seed(config["seed"]) 
torch.manual_seed(config["seed"])
random.seed(config["seed"])

if torch.cuda.is_available():
    torch.cuda.manual_seed(config["seed"])
    torch.cuda.manual_seed_all(config["seed"])
    torch.backends.cudnn.deterministic = True

dataset = pd.read_csv(f'datasets/{config["dataset"]}')
stop_words = StopWordRemoverFactory().get_stop_words()
tokenizer = BertTokenizer.from_pretrained(config["bert_model"])
stemmer = StemmerFactory().create_stemmer()
labels = sorted(dataset['prodi'].unique().tolist())
max_length = max(len(str(row[config["data"]]).split()) for row in dataset.to_dict('records')) + 5


# preprocessor
if not os.path.exists("datasets/train_set.pkl") and not os.path.exists("datasets/valid_set.pkl") and not os.path.exists("datasets/test_set.pkl"):
    print("\nPreprocessing Data...")
    input_ids, attention_mask, target = [], [], []

    for row in tqdm(dataset.to_dict('records'), desc="Preprocessing"):
        label = labels.index(row["prodi"])
        text = str(row[config["data"]]) 
        text = text.lower()
        text = emoji.replace_emoji(text, replace='') 
        text = re.sub(r'\n', ' ', text) 
        text = re.sub(r'http\S+', '', text)  
        text = re.sub(r'\d+', '', text)  
        text = re.sub(r'[^a-zA-Z ]', '', text)  
        text = ' '.join([word for word in text.split() if word not in stop_words])  
        text = stemmer.stem(text)
        text = text.strip()      

        token = tokenizer(text=text, max_length=max_length, padding="max_length", truncation=True)  
        input_ids.append(token['input_ids'])
        attention_mask.append(token['attention_mask'])
        target.append(label)

    input_ids = torch.tensor(input_ids)
    attention_mask = torch.tensor(attention_mask)
    target = torch.tensor(target)
    tensor_dataset = TensorDataset(input_ids, attention_mask, target)

    train_valid_size = round(len(tensor_dataset) * 0.8)
    test_size = len(tensor_dataset) - train_valid_size
    train_valid_set, test_set = torch.utils.data.random_split(tensor_dataset, [train_valid_size, test_size])

    train_size = round(len(train_valid_set) * 0.9)
    valid_size = len(train_valid_set) - train_size

    train_set, valid_set = torch.utils.data.random_split(train_valid_set, [train_size, valid_size])
    with open("datasets/train_set.pkl", 'wb') as train_preprocessed:
        pickle.dump(train_set, train_preprocessed)

    with open("datasets/valid_set.pkl", 'wb') as valid_preprocessed:
        pickle.dump(valid_set, valid_preprocessed)

    with open("datasets/test_set.pkl", 'wb') as test_preprocessed:
        pickle.dump(test_set, test_preprocessed)
    print('[ Preprocessing Completed ]\n')

print("\nLoading Data...")
with open("datasets/train_set.pkl", 'rb') as train_preprocessed:
    train_set = pickle.load(train_preprocessed)
    
with open("datasets/valid_set.pkl", 'rb') as valid_preprocessed:
    valid_set = pickle.load(valid_preprocessed)
    
with open("datasets/test_set.pkl", 'rb') as test_preprocessed:
    test_set = pickle.load(test_preprocessed)
print('[ Loading Completed ]\n')

train_loader = torch.utils.data.DataLoader(dataset=train_set, 
                                        batch_size=config["batch_size"], 
                                        shuffle=True,
                                        num_workers=multiprocessing.cpu_count())

valid_loader = torch.utils.data.DataLoader(dataset=valid_set, 
                                        batch_size=config["batch_size"], 
                                        shuffle=False,
                                        num_workers=multiprocessing.cpu_count())

test_loader = torch.utils.data.DataLoader(dataset=test_set, 
                                        batch_size=config["batch_size"], 
                                        shuffle=False,
                                        num_workers=multiprocessing.cpu_count())


# model
class BERT_CNN(nn.Module):
    def __init__(self, num_classes, bert_model, dropout, input_size=768, window_sizes=[1, 2, 3, 4, 5], in_channels=4, out_channels=32):
        super(BERT_CNN, self).__init__()
        self.pretrained_bert = BertModel.from_pretrained(bert_model, output_hidden_states=True)

        conv_layers = []
        for window_size in window_sizes:
            conv_layer = nn.Conv2d(in_channels, out_channels, (window_size, input_size))
            conv_layers.append(conv_layer)
        self.cnn = nn.ModuleList(conv_layers)

        self.dropout = nn.Dropout(dropout) 
        self.window_length = len(window_sizes)
        self.out_channels_length = out_channels
        self.output_layer = nn.Linear(len(window_sizes) * out_channels, num_classes)

    def forward(self, input_ids, attention_mask):
        bert_output = self.pretrained_bert(input_ids=input_ids, attention_mask=attention_mask)
        bert_hidden_states = bert_output[2]
        bert_hidden_states = torch.stack(bert_hidden_states, dim=1)
        stacked_hidden_states = bert_hidden_states[:, -4:]

        pooling = []
        for layer in self.cnn:
            hidden_states = layer(stacked_hidden_states)
            relu_output = F.relu(hidden_states.squeeze(3))
            pooling.append(relu_output)

        max_pooling = []
        for features in pooling:
            pooled_features = F.max_pool1d(features, features.size(2)).squeeze(2)
            max_pooling.append(pooled_features)
        
        flatten = torch.cat(max_pooling, dim=1)
        logits = self.dropout(flatten)
        preds = self.output_layer(logits)
        
        return preds
    
model = BERT_CNN(len(labels), config["bert_model"], config["dropout"])
model.to(device)

criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=config["lr"])


# fine-tune
best_loss = 9.99
failed_counter = 0

logger = pd.DataFrame(columns=['accuracy', 'loss', 'epoch', 'stage']) 
classification_report = pd.DataFrame(columns=['label', 'correct_prediction', 'false_prediction', 'total_prediction', 'epoch', 'stage'])

optimizer.zero_grad()
model.zero_grad()

print("Training Stage...")
for epoch in range(config["max_epochs"]):
    if failed_counter == config["patience"]:
        print("Early Stopping")
        break

    train_loss = 0
    n_batch = 0
    n_correct = 0
    n_samples = 0

    each_label_correct = defaultdict(int)
    each_label_total = defaultdict(int)

    model.train(True)
    for input_ids, attention_mask, target in tqdm(train_loader, desc="Training Stage", unit="batch"):
        input_ids = input_ids.to(device)
        attention_mask = attention_mask.to(device)
        target = target.to(device)

        preds = model(input_ids=input_ids, attention_mask=attention_mask)
        loss = criterion(preds, target)

        train_loss += loss.item()
        n_batch += 1

        result = torch.argmax(preds, dim=1) 
        n_correct += (result == target).sum().item()
        n_samples += target.size(0)

        for prediction, ground_truth in zip(result, target):
            if prediction == ground_truth:
                each_label_correct[ground_truth.item()] += 1
            each_label_total[ground_truth.item()] += 1

        loss.backward()
        optimizer.step()

        optimizer.zero_grad()
        model.zero_grad()

    train_loss /= n_batch
    acc = 100.0 * n_correct / n_samples
    logger = pd.concat([logger, pd.DataFrame({'accuracy': [acc], 'loss': [train_loss], 'epoch': [epoch+1], 'stage': ['train']})], ignore_index=True)
    print(f'Epoch [{epoch + 1}/{config["max_epochs"]}], Training Loss: {train_loss:.4f}, Training Accuracy: {acc:.2f}%')

    for label, correct_count in each_label_correct.items():
        total_count = each_label_total[label]
        false_count = total_count - correct_count
        classification_report = pd.concat([classification_report, pd.DataFrame({'label': [labels[label]], 'correct_prediction': [correct_count], 'false_prediction': [false_count], 'total_prediction': [total_count], 'epoch': [epoch+1], 'stage': ['train']})], ignore_index=True)
        print(f"Label: {labels[label]}, Correct Predictions: {correct_count}, False Predictions: {false_count}")

    model.eval()
    with torch.no_grad():
        val_loss = 0
        n_batch = 0
        n_correct = 0
        n_samples = 0

        each_label_correct = defaultdict(int)
        each_label_total = defaultdict(int)

        for input_ids, attention_mask, target in tqdm(valid_loader, desc="Validation Stage", unit="batch"):
            input_ids = input_ids.to(device)
            attention_mask = attention_mask.to(device)
            target = target.to(device)

            preds = model(input_ids=input_ids, attention_mask=attention_mask)
            loss = criterion(preds, target)

            val_loss += loss.item()
            n_batch += 1

            result = torch.argmax(preds, dim=1) 
            n_correct += (result == target).sum().item()
            n_samples += target.size(0)

            for prediction, ground_truth in zip(result, target):
                if prediction == ground_truth:
                    each_label_correct[ground_truth.item()] += 1
                each_label_total[ground_truth.item()] += 1

            optimizer.zero_grad()
            model.zero_grad()

        val_loss /= n_batch
        acc = 100.0 * n_correct / n_samples
        logger = pd.concat([logger, pd.DataFrame({'accuracy': [acc], 'loss': [val_loss], 'epoch': [epoch+1], 'stage': ['valid']})], ignore_index=True)
        print(f'Epoch [{epoch + 1}/{config["max_epochs"]}], Validation Loss: {val_loss:.4f}, Validation Accuracy: {acc:.2f}%')

        for label, correct_count in each_label_correct.items():
            total_count = each_label_total[label]
            false_count = total_count - correct_count
            classification_report = pd.concat([classification_report, pd.DataFrame({'label': [labels[label]], 'correct_prediction': [correct_count], 'false_prediction': [false_count], 'total_prediction': [total_count], 'epoch': [epoch+1], 'stage': ['valid']})], ignore_index=True)
            print(f"Label: {labels[label]}, Correct Predictions: {correct_count}, False Predictions: {false_count}")
        
        if round(val_loss, 2) < round(best_loss, 2):
            if not os.path.exists('checkpoints'):
                os.makedirs('checkpoints')

            if os.path.exists('checkpoints/model_result.pkl'):
                os.remove('checkpoints/model_result.pkl')

            checkpoint = {
                "epoch": epoch + 1,
                "model_state": model.state_dict(),
            }

            print("Saving Checkpoint...")   
            with open('checkpoints/model_result.pkl', 'wb') as temp:
                pickle.dump(checkpoint, temp)

            best_loss = val_loss
            failed_counter = 0

        else:
            failed_counter += 1

print("Test Stage...")
with open("checkpoints/model_result.pkl", 'rb') as checkpoint_path:
    pretrained_model = pickle.load(checkpoint_path)

print("Loading Checkpoint from Epoch", pretrained_model['epoch'])
model.load_state_dict(pretrained_model['model_state'])

model.eval()
with torch.no_grad():
    n_correct = 0
    n_samples = 0

    each_label_correct = defaultdict(int)
    each_label_total = defaultdict(int)

    for input_ids, attention_mask, target in tqdm(test_loader, desc="Test", unit="batch"):
        input_ids = input_ids.to(device)
        attention_mask = attention_mask.to(device)
        target = target.to(device)
        preds = model(input_ids=input_ids, attention_mask=attention_mask)

        result = torch.argmax(preds, dim=1) 
        n_samples += target.size(0)
        n_correct += (result == target).sum().item()

        for prediction, ground_truth in zip(result, target):
            if prediction == ground_truth:
                each_label_correct[ground_truth.item()] += 1
            each_label_total[ground_truth.item()] += 1

    acc = 100.0 * n_correct / n_samples
    logger = pd.concat([logger, pd.DataFrame({'accuracy': [acc], 'loss': ['-'], 'epoch': ['-'], 'stage': ['test']})], ignore_index=True)
    print(f'Test Accuracy: {acc:.2f}%')

    for label, correct_count in each_label_correct.items():
        total_count = each_label_total[label]
        false_count = total_count - correct_count
        classification_report = pd.concat([classification_report, pd.DataFrame({'label': [labels[label]], 'correct_prediction': [correct_count], 'false_prediction': [false_count], 'total_prediction': [total_count], 'epoch': ["-"], 'stage': ['test']})], ignore_index=True)
        print(f"Label: {labels[label]}, Correct Predictions: {correct_count}, False Predictions: {false_count}")

if not os.path.exists('logs'):
    os.makedirs('logs')

logger.to_csv('logs/metrics.csv', index=False, encoding='utf-8')
classification_report.to_csv('logs/classification_report.csv', index=False, encoding='utf-8')


# create graph
train_log = logger[logger['stage'] == 'train'].reset_index(drop=True)
valid_log = logger[logger['stage'] == 'valid'].reset_index(drop=True)
test_log = logger[logger['stage'] == 'test'].reset_index(drop=True)

plt.title('Test Accuracy: {:.2f}'.format(test_log['accuracy'][0]), ha='center', fontsize='medium')
plt.xlabel('Epoch')
plt.ylabel('Value')
plt.plot(train_log['epoch'], train_log['accuracy'], marker='o', label='Train Accuracy')
plt.plot(valid_log['epoch'], valid_log['accuracy'], marker='o', label='Validation Accuracy')
plt.plot(train_log['epoch'], train_log['loss'], marker='o', label='Train Loss')
plt.plot(valid_log['epoch'], valid_log['loss'], marker='o', label='Validation Loss')
plt.gca().xaxis.set_major_locator(mticker.MultipleLocator(1))

for metric in ['accuracy', 'loss']:
    for stage, logger in enumerate([train_log, valid_log]):
        for index, value in enumerate(logger[metric]):
            value_label = '{:.2f}'.format(value)
            plt.annotate(value_label,
                        (logger['epoch'][index], value),
                        textcoords='offset points',
                        xytext=(0, 4),
                        fontsize='small',
                        ha='right' if stage == 0 else 'left')

plt.legend()
plt.savefig('logs/metrics.png')
plt.clf()