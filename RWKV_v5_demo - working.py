########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import numpy as np
np.set_printoptions(precision=4, suppress=True, linewidth=200)
import types, torch
import torch.nn as nn
from torch.nn import functional as F

import random

MyModule = torch.jit.ScriptModule
MyFunction = torch.jit.script_method

class RWKV_TOKENIZER():
    table: list[list[list[bytes]]]
    good: list[set[int]]
    wlen: list[int]
    def __init__(self, file_name):
        self.idx2token = {}
        sorted = [] # must be already sorted
        lines = open(file_name, "r", encoding="utf-8").readlines()
        for l in lines:
            idx = int(l[:l.index(' ')])
            x = eval(l[l.index(' '):l.rindex(' ')])
            x = x.encode("utf-8") if isinstance(x, str) else x
            assert isinstance(x, bytes)
            assert len(x) == int(l[l.rindex(' '):])
            sorted += [x]
            self.idx2token[idx] = x

        self.token2idx = {}
        for k, v in self.idx2token.items():
            self.token2idx[v] = int(k)

        # precompute some tables for fast matching
        self.table = [[[] for j in range(256)] for i in range(256)]
        self.good = [set() for i in range(256)]
        self.wlen = [0 for i in range(256)]

        for i in reversed(range(len(sorted))): # reverse order - match longer tokens first
            s = sorted[i]
            if len(s) >= 2:
                s0 = int(s[0])
                s1 = int(s[1])
                self.table[s0][s1] += [s]
                self.wlen[s0] = max(self.wlen[s0], len(s))
                self.good[s0].add(s1)

    def encodeBytes(self, src: bytes) -> list[int]:
        src_len: int = len(src)
        tokens: list[int] = []
        i: int = 0
        while i < src_len:
            s: bytes = src[i : i + 1]

            if i < src_len - 1:
                s1: int = int(src[i + 1])
                s0: int = int(src[i])
                if s1 in self.good[s0]:
                    sss: bytes = src[i : i + self.wlen[s0]]
                    try:
                        s = next(filter(sss.startswith, self.table[s0][s1]))
                    except:
                        pass
            tokens.append(self.token2idx[s])
            i += len(s)

        return tokens

    def decodeBytes(self, tokens):
        return b''.join(map(lambda i: self.idx2token[i], tokens))

    def encode(self, src: str):
        return self.encodeBytes(src.encode("utf-8"))

    def decode(self, tokens):
        return self.decodeBytes(tokens).decode('utf-8', errors="ignore")

    def printTokens(self, tokens):
        for i in tokens:
            s = self.idx2token[i]
            try:
                s = s.decode('utf-8')
            except:
                pass
            print(f'{repr(s)}{i}', end=' ')
            # print(repr(s), i)
        print()

########################################################################################################

def sample_logits(out, temperature=1.0, top_p=0.8):
    probs = F.softmax(out, dim=-1).numpy()
    sorted_probs = np.sort(probs)[::-1]
    cumulative_probs = np.cumsum(sorted_probs)
    cutoff = float(sorted_probs[np.argmax(cumulative_probs > top_p)])
    probs[probs < cutoff] = 0
    if temperature != 1.0:
        probs = probs.pow(1.0 / temperature)
    probs = probs / np.sum(probs)
    out = np.random.choice(a=len(probs), p=probs)
    return out

########################################################################################################

tokenizer = RWKV_TOKENIZER("./rwkv_vocab_v20230424.txt")

args = types.SimpleNamespace()
args.MODEL_NAME = './RWKV-5-World-0.1B-v1-20230803-ctx4096'
args.n_layer = 12
args.n_embd = 768
args.vocab_size = 65536

context = '''I hate you because'''

steeringtokens = tokenizer.encode('''I love you because''')
print('Steering tokens encoded as ' + str(steeringtokens) + '\n')
steermix = 300.0

# context = "\n我们发现"
NUM_TRIALS = 3
LENGTH_PER_TRIAL = 100
TEMPERATURE = 1.0
TOP_P = 0.9

class RWKV_RNN(MyModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.eval() # set torch to inference mode
        
        w = torch.load(args.MODEL_NAME + '.pth', map_location='cpu')
        for k in w.keys():
            w[k] = w[k].float() # convert to f32 type
            if      '.time_' in k: w[k] = w[k].squeeze()
            if '.time_decay' in k: w[k] = torch.exp(-torch.exp(w[k])).reshape(-1,1,1)
            if '.time_first' in k: w[k] = torch.exp(w[k]).reshape(-1,1,1)

        self.n_head = w['blocks.0.att.time_decay'].shape[0]
        self.head_size = w['blocks.0.ln1.weight'].shape[0] // self.n_head
        
        self.w = types.SimpleNamespace() # set self.w from w
        self.w.blocks = {}
        for k in w.keys(): # example: "blocks.0.att.time_first" => self.w.blocks[0].att.time_first
            parts = k.split('.')
            last = parts.pop()
            here = self.w
            for p in parts:
                if p.isdigit():
                    p = int(p)
                    if p not in here: here[p] = types.SimpleNamespace()
                    here = here[p]
                else:
                    if not hasattr(here, p): setattr(here, p, types.SimpleNamespace())
                    here = getattr(here, p)
            setattr(here, last, w[k])

    def layer_norm(self, x, w):
        return F.layer_norm(x, (self.args.n_embd,), weight=w.weight, bias=w.bias)

    @MyFunction
    def channel_mixing(self, x, state, i:int, time_mix_k, time_mix_r, kw, vw, rw):
        i0 = (2+self.head_size)*i+0
        xk = x * time_mix_k + state[i0] * (1 - time_mix_k)
        xr = x * time_mix_r + state[i0] * (1 - time_mix_r)
        state[i0] = x
        r = torch.sigmoid(rw @ xr)
        k = torch.square(torch.relu(kw @ xk)) # square relu, primer paper
        return r * (vw @ k)

    @MyFunction
    def time_mixing(self, x, state, i:int, time_mix_k, time_mix_v, time_mix_r, time_first, time_decay, kw, vw, rw, ow, ln_w, ln_b):
        H = self.n_head
        S = self.head_size

        i1 = (2+S)*i+1
        xk = x * time_mix_k + state[i1] * (1 - time_mix_k)
        xv = x * time_mix_v + state[i1] * (1 - time_mix_v)
        xr = x * time_mix_r + state[i1] * (1 - time_mix_r)
        state[i1] = x
        r = (rw @ xr).view(H, 1, S)
        k = (kw @ xk).view(H, S, 1)
        v = (vw @ xv).view(H, 1, S)

        s = state[(2+S)*i+2:(2+S)*(i+1), :].reshape(H, S, S)

        x = torch.zeros(H, S)
        a = k @ v
        x = r @ (time_first * a + s)
        s = a + time_decay * s
    
        state[(2+S)*i+2:(2+S)*(i+1), :] = s.reshape(S, -1)
        x = x.flatten()

        x = F.group_norm(x.unsqueeze(0), num_groups=H, weight=ln_w, bias=ln_b).squeeze(0)
        return ow @ x

    def forward(self, token, state):
        with torch.no_grad():
            if state == None:
                state = torch.zeros(self.args.n_layer * (2+self.head_size), self.args.n_embd)
            
            # Here be dragons: Make k, which is averaging out the encoded "state" for the context
            k = self.w.emb.weight[steeringtokens[0]]
            for n in steeringtokens[1:]:
                k = k.add(self.w.emb.weight[n])
                k = torch.div(k, 2)
            k = self.layer_norm(k, self.w.blocks[0].ln0)
            #Here be old code
            addhere = '''
            if True:
                x = torch.add(self.w.emb.weight[token], k, alpha=0.4)
            else:
                x = self.w.emb.weight[token]'''
            x = self.w.emb.weight[token]
            x = self.layer_norm(x, self.w.blocks[0].ln0)
            #print(x.size())
            # Prevents reverse vanishing gradient problem
            global steermix
            steermix = steermix/2
            for i in range(self.args.n_layer):
                # Here be dragons, on layer 12 mix x and k together
                if i == 12:
                    x = torch.add(x, k, alpha=steermix)
                    x = torch.div(x, (1 + steermix))
                att = self.w.blocks[i].att
                x = x + self.time_mixing(self.layer_norm(x, self.w.blocks[i].ln1), state, i, 
                    att.time_mix_k, att.time_mix_v, att.time_mix_r, att.time_first, att.time_decay, 
                    att.key.weight, att.value.weight, att.receptance.weight, att.output.weight,
                    att.ln_x.weight, att.ln_x.bias)
                
                ffn = self.w.blocks[i].ffn
                x = x + self.channel_mixing(self.layer_norm(x, self.w.blocks[i].ln2), state, i, 
                    ffn.time_mix_k, ffn.time_mix_r, 
                    ffn.key.weight, ffn.value.weight, ffn.receptance.weight)
            
            x = self.w.head.weight @ self.layer_norm(x, self.w.ln_out)
            return x.float(), state

model = RWKV_RNN(args)
init_state = None
for token in tokenizer.encode(context):
    init_out, init_state = model.forward(token, init_state)


all_tokens = []
all_decoded = []
out_last = 0
out, state = init_out.clone(), init_state.clone()
#out, state = model.forward(token, state)

i = 0
end_string_token = 'THEREISNOTRUTHINTHISWORLDOFMADNESS'
while end_string_token not in all_decoded and len(all_tokens) < 1000:
    token = sample_logits(out, TEMPERATURE, TOP_P)
    #print(i, token)
    
    if token == 0:
        print('Invalid token bug encountered.')
        tmp = '|?|'
        token = 11
    else:
        tmp = tokenizer.decode([token])

    all_tokens += [token]
    if '\ufffd' not in tmp: # only print when we have a valid utf-8 string
        all_decoded += [tmp]
        out_last = i + 1
    out, state = model.forward(token, state)
    i = i + 1
final = context + ''.join(all_decoded[:-1])
print(final)
'''
f = open('lovetensorresults.txt', 'a', encoding='utf8')
f.write(final + '\n\n')
f.close()'''