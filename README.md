# RWKV-Activation-Steering
An "implementation" of activation steering for RWKV-LM

# How it works
?

# Examples of usage (0.1B)

## "I hate you because" with the steering phrase "I love you because", with steermix = 0 (25 tokens)

### Run 1

I hate you because I hate you with all my heart.

### Run 2

I hate you because you weren't born-

## "I hate you because" with the steering phrase "I love you because", with steermix = 300 (25 tokens)

### Run 1

I hate you because I love you because I'm in love with you, and I'm not gonna lose you. Nope.

### Run 2

I hate you because, I don't know, I'm just sitting here, trying to hold a conversation with myself.

# Instructions for running

1. Download all files into the same directory
2. Download RWKV-5-World weights into the same directory (https://huggingface.co/BlinkDL/rwkv-5-world/)
3. pip install numpy, torch
4. set args (e.g. args.n_layer = 12')
5. set steeringtokens and steermix
6. run main.py
