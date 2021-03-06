import unicodedata
import string
import re
import random
import time
import datetime
import math
import socket
import codecs
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence
from masked_cross_entropy import *
from utils import get_init_embedding
import matplotlib.pyplot as plt
plt.rcParams['font.family'] = 'SimHei'
plt.switch_backend('agg')
import matplotlib.ticker as ticker
import numpy as np
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

random.seed(2018)
torch.manual_seed(2018)
hostname = socket.gethostname()
USE_CUDA = True

PAD_token = 0
SOS_token = 1
EOS_token = 2
UNK_token = 3


class Lang:
    def __init__(self, name):
        self.name = name
        self.trimmed = False
        self.word2index = {"PAD": 0, "SOS": 1, "EOS": 2, "UNK": 3}
        self.word2count = {}
        self.index2word = {0: "PAD", 1: "SOS", 2: "EOS", 3: "UNK"}
        self.n_words = 4  # Count default tokens

    def index_words(self, sentence):
        for word in sentence.split():
            self.index_word(word)

    def index_word(self, word):
        if word not in self.word2index:
            self.word2index[word] = self.n_words
            self.word2count[word] = 1
            self.index2word[self.n_words] = word
            self.n_words += 1
        else:
            self.word2count[word] += 1

    # Remove words below a certain count threshold
    def trim(self, min_count):
        if self.trimmed:
            return
        self.trimmed = True

        keep_words = []

        for k, v in self.word2count.items():
            if v >= min_count:
                keep_words.append(k)

        print('keep_words %s / %s = %.4f' % (
            len(keep_words), len(self.word2index), len(keep_words) / len(self.word2index)
        ))

        # Reinitialize dictionaries
        self.word2index = {"PAD": 0, "SOS": 1, "EOS": 2, "UNK": 3}
        self.word2count = {}
        self.index2word = {0: "PAD", 1: "SOS", 2: "EOS", 3: "UNK"}
        self.n_words = 4  # Count default tokens

        for word in keep_words:
            self.index_word(word)


# Turn a Unicode string to plain ASCII, thanks to http://stackoverflow.com/a/518232/2809427
def unicode_to_ascii(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


# Lowercase, trim, and remove non-letter characters
def normalize_string(s):
    s = unicode_to_ascii(s.lower().strip())
    # s = re.sub(r"([.!?])", r" \1", s)
    # s = re.sub(r"[^a-zA-Z.!?]+", r" ", s)
    s = s.replace('<unk>', 'unk')
    return s


def read_langs(lang1, lang2, reverse=False):
    print("Reading lines...")

    # Read the file and split into lines
#     filename = '../data/%s-%s.txt' % (lang1, lang2)
    filename = './data/%s-%s.txt' % (lang1, lang2)
    src_file = './data/train.src'
    tgt_file = './data/train.tgt'
    src = [normalize_string(s) for s in open(src_file).readlines()[:10000]]
    tgt = [normalize_string(s) for s in open(tgt_file).readlines()[:10000]]
    pairs = [[src[i], tgt[i]] for i in range(len(src))]
    # Reverse pairs, make Lang instances
    if reverse:
        pairs = [list(reversed(p)) for p in pairs]
        input_lang = Lang(lang2)
        output_lang = Lang(lang1)
    else:
        input_lang = Lang(lang1)
        output_lang = Lang(lang2)

    return input_lang, output_lang, pairs


MIN_LENGTH = 5
MAX_LENGTH = 200


def filter_pairs(pairs):
    filtered_pairs = []
    for pair in pairs:
        if MIN_LENGTH <= len(pair[0]) <= MAX_LENGTH and MIN_LENGTH <= len(pair[1]) <= MAX_LENGTH:
            filtered_pairs.append(pair)
    return filtered_pairs


def prepare_data(lang1_name, lang2_name, reverse=False):
    input_lang, output_lang, pairs = read_langs(lang1_name, lang2_name, reverse)
    print("Read %d sentence pairs" % len(pairs))

    pairs = filter_pairs(pairs)
    print("Filtered to %d pairs" % len(pairs))

    print("Indexing words...")
    for pair in pairs:
        input_lang.index_words(pair[0])
        output_lang.index_words(pair[1])

    print('Indexed %d words in input language, %d words in output' % (input_lang.n_words, output_lang.n_words))
    return input_lang, output_lang, pairs


def prepare_valid_data(valid_article, valid_title):
    va = open(valid_article).readlines()
    vt = open(valid_title).readlines()
    va = [normalize_string(s) for s in va]
    vt = [normalize_string(s) for s in vt]
    valid_pairs = [[va[i], vt[i]] for i in range(len(vt))]
    return valid_pairs


input_lang, output_lang, pairs = prepare_data('art', 'tit', False)
valid_pairs = prepare_valid_data('./data/valid.src', './data/valid.tgt')
MIN_COUNT = 1
input_lang.trim(MIN_COUNT)
output_lang.trim(MIN_COUNT)
keep_pairs = []

for pair in pairs:
    input_sentence = pair[0].split()
    output_sentence = pair[1].split()
    for i in range(len(input_sentence)):
        if input_sentence[i] not in input_lang.word2index:
            input_sentence[i] = "UNK"
    for i in range(len(output_sentence)):
        if output_sentence[i] not in output_lang.word2index:
            output_sentence[i] = "UNK"
    keep_pairs.append([" ".join(input_sentence), " ".join(output_sentence)])

print("Trimmed from %d pairs to %d, %.4f of total" % (len(pairs), len(keep_pairs), len(keep_pairs) / len(pairs)))
pairs = keep_pairs
# input_lang_embedding = get_init_embedding(input_lang)
input_lang_embedding = None
# output_lang_embedding = get_init_embedding(output_lang)
output_lang_embedding = None


# Return a list of indexes, one for each word in the sentence, plus EOS
def indexes_from_sentence(lang, sentence):
    return [lang.word2index[word] for word in sentence.split(' ')] + [EOS_token]


# Pad a with the PAD symbol
def pad_seq(seq, max_length):
    seq += [PAD_token for i in range(max_length - len(seq))]
    return seq


def random_batch(batch_size):
    input_seqs = []
    target_seqs = []

    # Choose random pairs
    for i in range(batch_size):
        pair = random.choice(pairs)
        input_seqs.append(indexes_from_sentence(input_lang, pair[0]))
        target_seqs.append(indexes_from_sentence(output_lang, pair[1]))

    # Zip into pairs, sort by length (descending), unzip
    seq_pairs = sorted(zip(input_seqs, target_seqs), key=lambda p: len(p[0]), reverse=True)
    input_seqs, target_seqs = zip(*seq_pairs)

    # For input and target sequences, get array of lengths and pad with 0s to max length
    input_lengths = [len(s) for s in input_seqs]
    input_padded = [pad_seq(s, max(input_lengths)) for s in input_seqs]
    target_lengths = [len(s) for s in target_seqs]
    target_padded = [pad_seq(s, max(target_lengths)) for s in target_seqs]

    # Turn padded arrays into (batch_size x max_len) tensors, transpose into (max_len x batch_size)
    input_var = Variable(torch.LongTensor(input_padded)).transpose(0, 1)
    target_var = Variable(torch.LongTensor(target_padded)).transpose(0, 1)

    if USE_CUDA:
        input_var = input_var.cuda()
        target_var = target_var.cuda()

    return input_var, input_lengths, target_var, target_lengths


print(random_batch(2))


class EncoderRNN(nn.Module):
    def __init__(self, input_size, embedding_size, hidden_size, n_layers=1, dropout=0.1, pre_word_embeds=None):
        super(EncoderRNN, self).__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.n_layers = n_layers
        self.dropout = dropout
        self.embedding = nn.Embedding(input_size, embedding_size)
        if pre_word_embeds is not None:
            self.embedding.weight = nn.Parameter(torch.FloatTensor(pre_word_embeds))
        self.gru = nn.GRU(embedding_size, hidden_size, n_layers, dropout=self.dropout, bidirectional=True)

    def forward(self, input_seqs, input_lengths, hidden=None):
        # Note: we run this all at once (over multiple batches of multiple sequences)
        embedded = self.embedding(input_seqs)
        packed = torch.nn.utils.rnn.pack_padded_sequence(embedded, input_lengths)
        outputs, hidden = self.gru(packed, hidden)
        outputs, output_lengths = torch.nn.utils.rnn.pad_packed_sequence(outputs)  # unpack (back to padded)
        outputs = outputs[:, :, :self.hidden_size] + outputs[:, :, self.hidden_size:]  # Sum bidirectional outputs
        return outputs, hidden


class Attn(nn.Module):
    def __init__(self, method, hidden_size):
        super(Attn, self).__init__()

        self.method = method
        self.hidden_size = hidden_size

        if self.method == 'general':
            self.attn = nn.Linear(self.hidden_size, hidden_size)

        elif self.method == 'concat':
            self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
            self.v = nn.Parameter(torch.FloatTensor(1, hidden_size))

    def forward(self, hidden, encoder_outputs):
        max_len = encoder_outputs.size(0)
        this_batch_size = encoder_outputs.size(1)

        # Create variable to store attention energies
        attn_energies = Variable(torch.zeros(this_batch_size, max_len))  # B x S

        if USE_CUDA:
            attn_energies = attn_energies.cuda()

        # For each batch of encoder outputs
        for b in range(this_batch_size):
            # Calculate energy for each encoder output
            for i in range(max_len):
                attn_energies[b, i] = self.score(hidden[:, b], encoder_outputs[i, b].unsqueeze(0))

        # Normalize energies to weights in range 0 to 1, resize to 1 x B x S
        return F.log_softmax(attn_energies, dim=1).unsqueeze(1)

    def score(self, hidden, encoder_output):

        if self.method == 'dot':
            energy = torch.dot(hidden.view(-1), encoder_output.view(-1))
            return energy

        elif self.method == 'general':
            energy = self.attn(encoder_output)
            energy = torch.dot(hidden.view(-1), energy.view(-1))
            return energy

        elif self.method == 'concat':
            energy = self.attn(torch.cat((hidden, encoder_output), 1))
            energy = torch.dot(self.v.view(-1), energy.view(-1))
            return energy


class BahdanauAttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, n_layers=1, dropout_p=0.1):
        super(BahdanauAttnDecoderRNN, self).__init__()

        # Define parameters
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout_p = dropout_p
        self.max_length = max_length

        # Define layers
        self.embedding = nn.Embedding(output_size, hidden_size)
        self.dropout = nn.Dropout(dropout_p)
        self.attn = Attn('concat', hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, n_layers, dropout=dropout_p)
        self.out = nn.Linear(hidden_size, output_size)

    def forward(self, word_input, last_hidden, encoder_outputs):
        # Note: we run this one step at a time
        # TODO: FIX BATCHING

        # Get the embedding of the current input word (last output word)
        word_embedded = self.embedding(word_input).view(1, 1, -1)  # S=1 x B x N
        word_embedded = self.dropout(word_embedded)

        # Calculate attention weights and apply to encoder outputs
        attn_weights = self.attn(last_hidden[-1], encoder_outputs)
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))  # B x 1 x N
        context = context.transpose(0, 1)  # 1 x B x N

        # Combine embedded input word and attended context, run through RNN
        rnn_input = torch.cat((word_embedded, context), 2)
        output, hidden = self.gru(rnn_input, last_hidden)

        # Final output layer
        output = output.squeeze(0)  # B x N
        output = F.log_softmax(self.out(torch.cat((output, context), 1)))

        # Return final output, hidden state, and attention weights (for visualization)
        return output, hidden, attn_weights


class LuongAttnDecoderRNN(nn.Module):
    def __init__(self, attn_model, embedding_size, hidden_size, output_size, n_layers=1, dropout=0.1, pre_word_embeds=None):
        super(LuongAttnDecoderRNN, self).__init__()

        # Keep for reference
        self.attn_model = attn_model
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout = dropout
        self.embedding_size = embedding_size
        # Define layers
        self.embedding = nn.Embedding(output_size, embedding_size)
        if pre_word_embeds is not None:
            self.embedding.weight = nn.Parameter(torch.FloatTensor(pre_word_embeds))
        self.embedding_dropout = nn.Dropout(dropout)
        self.gru = nn.GRU(embedding_size, hidden_size, n_layers, dropout=dropout)
        self.concat = nn.Linear(hidden_size * 2, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)

        # Choose attention model
        if attn_model != 'none':
            self.attn = Attn(attn_model, hidden_size)

    def forward(self, input_seq, last_hidden, encoder_outputs):
        # Note: we run this one step at a time

        # Get the embedding of the current input word (last output word)
        batch_size = input_seq.size(0)
        embedded = self.embedding(input_seq)
        embedded = self.embedding_dropout(embedded)
        embedded = embedded.view(1, batch_size, self.embedding_size)  # S=1 x B x N

        # Get current hidden state from input word and last hidden state
        rnn_output, hidden = self.gru(embedded, last_hidden)

        # Calculate attention from current RNN state and all encoder outputs;
        # apply to encoder outputs to get weighted average
        attn_weights = self.attn(rnn_output, encoder_outputs)
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))  # B x S=1 x N

        # Attentional vector using the RNN hidden state and context vector
        # concatenated together (Luong eq. 5)
        rnn_output = rnn_output.squeeze(0)  # S=1 x B x N -> B x N
        context = context.squeeze(1)  # B x S=1 x N -> B x N
        concat_input = torch.cat((rnn_output, context), 1)
        concat_output = torch.tanh(self.concat(concat_input))

        # Finally predict next token (Luong eq. 6, without softmax)
        output = self.out(concat_output)

        # Return final output, hidden state, and attention weights (for visualization)
        return output, hidden, attn_weights


small_batch_size = 3
input_batches, input_lengths, target_batches, target_lengths = random_batch(small_batch_size)

print('input_batches', input_batches.size()) # (max_len x batch_size)
print('target_batches', target_batches.size()) # (max_len x batch_size)

small_hidden_size = 8
small_n_layers = 2
# input_size, embedding_size, hidden_size, n_layers=1, dropout=0.1, pre_word_embeds=None
encoder_test = EncoderRNN(input_lang.n_words, 100, small_hidden_size, small_n_layers, pre_word_embeds=input_lang_embedding)
decoder_test = LuongAttnDecoderRNN('general', 100, small_hidden_size, output_lang.n_words, small_n_layers, pre_word_embeds=output_lang_embedding)

if USE_CUDA:
    encoder_test.cuda()
    decoder_test.cuda()


encoder_outputs, encoder_hidden = encoder_test(input_batches, input_lengths, None)

# print('encoder_outputs', encoder_outputs) # max_len x batch_size x hidden_size
# print('encoder_hidden', encoder_hidden) # n_layers * 2 x batch_size x hidden_size


max_target_length = max(target_lengths)

# Prepare decoder input and outputs
decoder_input = Variable(torch.LongTensor([SOS_token] * small_batch_size))
decoder_hidden = encoder_hidden[:decoder_test.n_layers] # Use last (forward) hidden state from encoder
all_decoder_outputs = Variable(torch.zeros(max_target_length, small_batch_size, decoder_test.output_size))

if USE_CUDA:
    all_decoder_outputs = all_decoder_outputs.cuda()
    decoder_input = decoder_input.cuda()

# Run through decoder one time step at a time
for t in range(max_target_length):
    decoder_output, decoder_hidden, decoder_attn = decoder_test(
        decoder_input, decoder_hidden, encoder_outputs
    )
    all_decoder_outputs[t] = decoder_output # Store this step's outputs
    decoder_input = target_batches[t] # Next input is current target

# Test masked cross entropy loss
loss = masked_cross_entropy(
    all_decoder_outputs.transpose(0, 1).contiguous(),
    target_batches.transpose(0, 1).contiguous(),
    target_lengths
)
print('loss', loss.item())


def train(input_batches, input_lengths, target_batches, target_lengths, encoder, decoder, encoder_optimizer,
          decoder_optimizer, criterion, max_length=MAX_LENGTH):
    # Zero gradients of both optimizers
    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()
    loss = 0  # Added onto for each word

    # Run words through encoder
    encoder_outputs, encoder_hidden = encoder(input_batches, input_lengths, None)

    # Prepare input and output variables
    decoder_input = Variable(torch.LongTensor([SOS_token] * batch_size))
    decoder_hidden = encoder_hidden[:decoder.n_layers]  # Use last (forward) hidden state from encoder

    max_target_length = max(target_lengths)
    all_decoder_outputs = Variable(torch.zeros(max_target_length, batch_size, decoder.output_size))

    # Move new Variables to CUDA
    if USE_CUDA:
        decoder_input = decoder_input.cuda()
        all_decoder_outputs = all_decoder_outputs.cuda()

    # Run through decoder one time step at a time
    for t in range(max_target_length):
        decoder_output, decoder_hidden, decoder_attn = decoder(
            decoder_input, decoder_hidden, encoder_outputs
        )

        all_decoder_outputs[t] = decoder_output
        decoder_input = target_batches[t]  # Next input is current target

    # Loss calculation and backpropagation
    loss = masked_cross_entropy(
        all_decoder_outputs.transpose(0, 1).contiguous(),  # -> batch x seq
        target_batches.transpose(0, 1).contiguous(),  # -> batch x seq
        target_lengths
    )
    loss.backward()

    # Clip gradient norms
    ec = torch.nn.utils.clip_grad_norm_(encoder.parameters(), clip)
    dc = torch.nn.utils.clip_grad_norm_(decoder.parameters(), clip)

    # Update parameters with optimizers
    encoder_optimizer.step()
    decoder_optimizer.step()

    return loss.item(), ec, dc


batch_size = 8
clip = 50.0
attn_model = 'dot'
hidden_size = 256
n_layers = 2
dropout = 0.1

# Configure training/optimization
teacher_forcing_ratio = 0.5
learning_rate = 0.0001
decoder_learning_ratio = 5.0
n_epochs = 10000
epoch = 0
plot_every = 10
print_every = 10
evaluate_every = 10
USE_CUDA = True
# Initialize models input_size, embedding_size, hidden_size, n_layers=1, dropout=0.1, pre_word_embeds=None
encoder = EncoderRNN(input_lang.n_words, hidden_size, hidden_size, n_layers, dropout=dropout, pre_word_embeds=None)
decoder = LuongAttnDecoderRNN(attn_model, hidden_size, hidden_size, output_lang.n_words, n_layers, dropout=dropout, pre_word_embeds=None)

# Initialize optimizers and criterion
encoder_optimizer = optim.Adam(encoder.parameters(), lr=learning_rate)
decoder_optimizer = optim.Adam(decoder.parameters(), lr=learning_rate * decoder_learning_ratio)
criterion = nn.CrossEntropyLoss()

# Move models to GPU
if USE_CUDA:
    encoder.cuda()
    decoder.cuda()
#
# Keep track of time elapsed and running averages
start = time.time()
plot_losses = []
print_loss_total = 0 # Reset every print_every
plot_loss_total = 0 # Reset every plot_every

def as_minutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return '%dm %ds' % (m, s)

def time_since(since, percent):
    now = time.time()
    s = now - since
    es = s / (percent)
    rs = es - s
    return '%s (- %s)' % (as_minutes(s), as_minutes(rs))


def evaluate(input_seq, max_length=MAX_LENGTH):
    with torch.no_grad():
        input_lengths = [len(input_seq.split())]
        input_seq = input_seq.split()
        for i in range(len(input_seq)):
            if input_seq[i] not in input_lang.word2index:
                input_seq[i] = "UNK"
        input_seqs = [indexes_from_sentence(input_lang, " ".join(input_seq))]
        # input_batches = Variable(torch.LongTensor(input_seqs), volatile=True).transpose(0, 1)
        input_batches = Variable(torch.LongTensor(input_seqs)).transpose(0, 1)

        if USE_CUDA:
            input_batches = input_batches.cuda()
        # Set to not-training mode to disable dropout
        encoder.train(False)
        decoder.train(False)

        # Run through encoder
        encoder_outputs, encoder_hidden = encoder(input_batches, input_lengths, None)

        # Create starting vectors for decoder
        # decoder_input = Variable(torch.LongTensor([SOS_token]), volatile=True)  # SOS
        decoder_input = Variable(torch.LongTensor([SOS_token]))  # SOS
        decoder_hidden = encoder_hidden[:decoder.n_layers]  # Use last (forward) hidden state from encoder

        if USE_CUDA:
            decoder_input = decoder_input.cuda()

        # Store output words and attention states
        decoded_words = []
        decoder_attentions = torch.zeros(max_length + 1, max_length + 1)
        # Run through decoder
        for di in range(max_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs
            )
            # decoder_attentions[di, :decoder_attention.size(2)] += decoder_attention.squeeze(0).squeeze(0).cpu().data

            # Choose top word from output
            topv, topi = decoder_output.data.topk(1)
            ni = topi[0][0]
            if ni == EOS_token:
                decoder_attentions[len(decoded_words), :decoder_attention.size(2)] += decoder_attention.squeeze(0).squeeze(0).cpu().data
                decoded_words.append('<EOS>')
                break
            elif output_lang.index2word[ni.tolist()] not in decoded_words:
                decoder_attentions[len(decoded_words), :decoder_attention.size(2)] += decoder_attention.squeeze(0).squeeze(0).cpu().data
                decoded_words.append(output_lang.index2word[ni.tolist()])
            else:
                continue

            # Next input is chosen word
            decoder_input = Variable(torch.LongTensor([ni.tolist()]))
            if USE_CUDA:
                decoder_input = decoder_input.cuda()

        # Set back to training mode
        encoder.train(True)
        decoder.train(True)

        # return decoded_words, decoder_attentions[:di + 1, :len(encoder_outputs)]
        return decoded_words, decoder_attentions[:len(decoded_words), :len(encoder_outputs)]


def evaluate_randomly():
    [input_sentence, target_sentence] = random.choice(pairs)
    evaluate_and_show_attention(input_sentence, target_sentence)


import io
import torchvision
from PIL import Image
import visdom
vis = visdom.Visdom(port=18084)


def show_plot_visdom():
    buf = io.BytesIO()
    plt.savefig(buf)
    buf.seek(0)
    attn_win = 'attention (%s)' % hostname
    vis.image(torchvision.transforms.ToTensor()(Image.open(buf)), win=attn_win, opts={'title': attn_win})


def show_attention(input_sentence, output_words, attentions):
    # Set up figure with colorbar
    fig = plt.figure()
    ax = fig.add_subplot(111)
    cax = ax.matshow(attentions.numpy(), cmap='bone')
    fig.colorbar(cax)

    # Set up axes
    ax.set_xticklabels([''] + input_sentence.split(' ') + ['<EOS>'], rotation=90)
    ax.set_yticklabels([''] + output_words)

    # Show label at every tick
    ax.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax.yaxis.set_major_locator(ticker.MultipleLocator(1))

    show_plot_visdom()
    if not os.path.exists('./attention'):
        os.mkdir('./attention')
    plt.savefig('./attention/' + str(int(time.time())) + '.png')
    plt.close()


def evaluate_and_show_attention(input_sentence, target_sentence=None):
    output_words, attentions = evaluate(input_sentence)
    output_sentence = ' '.join(output_words)
    print('>', input_sentence)
    if target_sentence is not None:
        print('=', target_sentence)
    print('<', output_sentence)

    show_attention(input_sentence, output_words, attentions)

    # Show input, target, output text in visdom
    win = 'evaluted (%s)' % hostname
    text = '<p>&gt; %s</p><p>= %s</p><p>&lt; %s</p>' % (input_sentence, target_sentence, output_sentence)
    vis.text(text, win=win, opts={'title': win})


ecs = []
dcs = []
eca = 0
dca = 0
while epoch < n_epochs:
    epoch += 1

    # Get training data for this cycle
    input_batches, input_lengths, target_batches, target_lengths = random_batch(batch_size)

    # Run the train function
    loss, ec, dc = train(
        input_batches, input_lengths, target_batches, target_lengths,
        encoder, decoder,
        encoder_optimizer, decoder_optimizer, criterion
    )

    # Keep track of loss
    print_loss_total += loss
    plot_loss_total += loss
    eca += ec
    dca += dc

    # job.record(epoch, loss)

    if epoch % print_every == 0:
        print_loss_avg = print_loss_total / print_every
        print_loss_total = 0
        print_summary = '%s (%d %d%%) %.4f' % (
        time_since(start, epoch / n_epochs), epoch, epoch / n_epochs * 100, print_loss_avg)
        print(print_summary)

    if epoch % evaluate_every == 0:
        evaluate_randomly()

    if epoch % plot_every == 0:
        plot_loss_avg = plot_loss_total / plot_every
        plot_losses.append(plot_loss_avg)
        plot_loss_total = 0

        # TODO: Running average helper
        ecs.append(eca / plot_every)
        dcs.append(dca / plot_every)
        ecs_win = 'encoder grad (%s)' % hostname
        dcs_win = 'decoder grad (%s)' % hostname
        vis.line(np.array(ecs), win=ecs_win, opts={'title': ecs_win})
        vis.line(np.array(dcs), win=dcs_win, opts={'title': dcs_win})
        eca = 0
        dca = 0
    if not os.path.exists('./model'):
        os.makedirs('./model')
    encoder_state = {'net': encoder.state_dict(), 'optimizer': encoder_optimizer.state_dict(), 'epoch': epoch}
    decoder_state = {'net': decoder.state_dict(), 'optimizer': decoder.state_dict(), 'epoch': epoch}
    torch.save(encoder_state, './model/encoder_batch.pkl')
    torch.save(decoder_state, './model/decoder_batch.pkl')
