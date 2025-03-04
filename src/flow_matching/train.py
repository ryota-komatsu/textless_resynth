import os
import sys
import warnings
from pathlib import Path

import jiwer
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, FastSpeech2ConformerHifiGan, pipeline

from .configs import ConditionalFlowMatchingConfig
from .data import UnitDataset
from .models import ConditionalFlowMatchingModel
from .utils.misc import cer_transform, fix_random_seed, get_lr_schedule, wer_transform
from .utils.textless import embedding

sys.path.append("src/utmos")
warnings.simplefilter("ignore", FutureWarning)
warnings.simplefilter("ignore", DeprecationWarning)
from ..utmos.score import Score


@torch.inference_mode()
def validate(config, dataloader, model, step, writer):
    model.eval()

    torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32

    vocoder = FastSpeech2ConformerHifiGan.from_pretrained(config.hifigan.path).cuda()
    asr = AutoModelForSpeechSeq2Seq.from_pretrained(
        config.asr.name,
        torch_dtype=torch_dtype,
        low_cpu_mem_usage=True,
        use_safetensors=True,
        device_map="cuda",
    )
    processor = AutoProcessor.from_pretrained(config.asr.name)
    pipe = pipeline(
        "automatic-speech-recognition",
        model=asr,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=torch_dtype,
    )
    scorer = Score(ckpt_path="src/utmos/epoch=3-step=7459.ckpt", input_sample_rate=16000, device="cuda")

    hyps = []
    refs = []
    hyp_scores = []
    ref_scores = []

    for n, batch in enumerate(dataloader):
        spectrogram = model.sample(input_ids=batch["input_ids"].cuda())
        hyp_wav = vocoder(spectrogram)

        hyp_score = scorer.score(hyp_wav)
        ref_score = scorer.score(batch["input_values"].cuda())

        hyp_wav = hyp_wav.cpu().squeeze(0).numpy()
        ref_wav = batch["input_values"].squeeze(0).numpy()

        hyp = pipe(hyp_wav, generate_kwargs={"language": "english"}, return_timestamps=True)["text"]
        ref = pipe(ref_wav, generate_kwargs={"language": "english"}, return_timestamps=True)["text"]

        hyps.append(hyp)
        refs.append(ref)
        hyp_scores.append(hyp_score)
        ref_scores.append(ref_score)

        if n < 5:
            writer.add_audio(f"hyp/{batch['names'][0]}", hyp_wav, step, 16000)
            writer.add_audio(f"ref/{batch['names'][0]}", ref_wav, step, 16000)

    wer_hyp = jiwer.wer(dataloader.dataset.transcripts, hyps, wer_transform, wer_transform)
    cer_hyp = jiwer.cer(dataloader.dataset.transcripts, hyps, cer_transform, cer_transform)
    mos_hyp = np.mean(hyp_scores)

    writer.add_scalar("dev/WER", wer_hyp, step)
    writer.add_scalar("dev/CER", cer_hyp, step)
    writer.add_scalar("dev/MOS", mos_hyp, step)

    wer_ref = jiwer.wer(dataloader.dataset.transcripts, refs, wer_transform, wer_transform)
    cer_ref = jiwer.cer(dataloader.dataset.transcripts, refs, cer_transform, cer_transform)
    mos_ref = np.mean(ref_scores)

    writer.add_scalar("dev/WER (REF)", wer_ref, step)
    writer.add_scalar("dev/CER (REF)", cer_ref, step)
    writer.add_scalar("dev/MOS (REF)", mos_ref, step)

    del vocoder
    del pipe
    del asr
    del scorer
    torch.cuda.empty_cache()


def train_flow_matching(config):
    fix_random_seed(config.common.seed)

    train_set = UnitDataset(
        config.dataset.train_file,
        spectrogram_dir=config.dataset.spectrogram_dir,
        frames_per_seg=config.flow_matching.frames_per_seg,
        ext_audio=config.dataset.ext_audio,
    )
    dev_set = UnitDataset(
        config.dataset.dev_file,
        config.dataset.wav_dir,
        ext_audio=config.dataset.ext_audio,
    )
    train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=config.flow_matching.batch_size,
        shuffle=True,
        num_workers=config.flow_matching.num_workers,
    )
    dev_loader = torch.utils.data.DataLoader(
        dev_set,
        num_workers=config.flow_matching.num_workers,
    )

    model = ConditionalFlowMatchingModel(
        ConditionalFlowMatchingConfig(vocab_size=config.flow_matching.vocab_size),
        embedding(
            config.flow_matching.dense_model_name,
            config.flow_matching.quantizer_model_name,
            config.flow_matching.vocab_size,
        ),
    ).cuda()

    optimizer = torch.optim.AdamW(model.parameters(), lr=config.flow_matching.lr, betas=(0.9, 0.98), eps=1e-9)

    # learning rate scheduler
    lr_scheduler = get_lr_schedule(
        optimizer,
        config.flow_matching.epoch * len(train_loader),
        config.flow_matching.warmup_steps,
        config.flow_matching.lr,
        config.flow_matching.lr_min,
    )

    scaler = torch.amp.GradScaler("cuda", init_scale=1e24)
    writer = SummaryWriter(os.path.join(config.flow_matching.path, "logs"))

    last_epoch = 0
    step = 0

    for epoch in range(last_epoch + 1, config.flow_matching.epoch + 1):
        model.train()

        for batch in train_loader:
            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                loss = model(
                    input_ids=batch["input_ids"].cuda(),
                    spectrogram_labels=batch["spectrogram_labels"].cuda(),
                )
            scaler.scale(loss).backward()

            # gradient clipping
            if config.flow_matching.max_norm is not None:
                scaler.unscale_(optimizer)
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config.flow_matching.max_norm)

            scaler.step(optimizer)
            scale = scaler.get_scale()
            scaler.update()
            optimizer.zero_grad()

            # update learning rate
            lr = lr_scheduler.get_last_lr()[0]
            lr_scheduler.step()

            step += 1

            # tensorboard log
            if step % config.flow_matching.summary_interval == 0:
                writer.add_scalar("train/loss", loss.item(), step)
                writer.add_scalar("train/lr", lr, step)
                writer.add_scalar("train/scale", scale, step)
                if config.flow_matching.max_norm is not None:
                    writer.add_scalar("train/grad_norm", grad_norm.item(), step)

        if epoch % config.flow_matching.save_interval_epoch == 0:
            validate(config, dev_loader, model, step, writer)

            # save model
            Path(config.flow_matching.path).parent.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(config.flow_matching.path)

            ckpt = {
                "epoch": epoch,
                "step": step,
                "optimizer": optimizer.state_dict(),
                "scheduler": lr_scheduler.state_dict(),
                "scaler": scaler.state_dict(),
            }
            torch.save(ckpt, os.path.join(config.flow_matching.path, "checkpoint"))
