# Linux VM (Proxmox) with captions sent over TCP

This setup runs SpankVANC headless on a Linux virtual machine, such as one on
Proxmox VE, without a DeckLink card. Program audio comes in over IP. Whisper
turns it into text, and SpankVANC sends that text over a **raw TCP socket**
(Telnet-style plain text) to an external caption encoder. The encoder embeds
the captions into SDI or the stream, replacing SpankVANC's own DeckLink VANC
embedding.

```
UDP / SRT / RTMP / file ──► FFmpeg (stock) ──► Whisper (CPU) ──► TCP text ──► caption encoder
```

Only a distribution FFmpeg and a Python Whisper environment are needed. The
patched DeckLink engine, Blackmagic SDK and Desktop Video drivers are **not**
required for this route.

## What works on a VM

| Feature | On a VM without DeckLink |
| --- | --- |
| UDP / SRT / RTMP / file input | Yes, with distribution FFmpeg |
| Whisper captions → **Caption text · TCP / Telnet** output | Yes, on CPU or a passed-through NVIDIA GPU (CUDA) |
| UDP / SRT / file / RTMP / YouTube media outputs | Yes, alongside the TCP output |
| Caption pass / strip on IP outputs | Depends on the installed FFmpeg (preflight checks it) |
| DeckLink SDI in/out, VANC caption embedding, SCTE-104 | Only with PCIe passthrough of the card plus the local engine build |
| Whisper captions embedded in IP outputs (`spankvanc_cc` filter) | Only with the locally built engine |

## 1. Create the VM in Proxmox

These settings are a starting point; adjust them to the model and channel count.

- **OS:** Ubuntu Server 24.04 LTS or Debian 12.
- **CPU type: `host`.** The default `kvm64`/`x86-64-v2-AES` types can hide
  AVX2/FMA from PyTorch, which makes Whisper several times slower.
- **Cores / RAM:** 4–8 cores and 8 GB RAM for `small.en` on CPU. Use `base.en`
  on smaller VMs. `medium`/`large-v3`/`turbo` need a passed-through NVIDIA GPU
  for live use.
- **Disk:** 32 GB is enough. Models are cached under the model cache folder.
- **Network:** VirtIO NIC on the bridge that carries the program stream. For
  multicast UDP input, check IGMP snooping/querier settings on the bridge and
  the physical switch.
- **GPU (recommended for live captions):** pass an NVIDIA card through to the
  VM. See [NVIDIA GPU for caption generation](#nvidia-gpu-for-caption-generation).

## 2. Install SpankVANC and FFmpeg

```bash
sudo apt update && sudo apt install -y python3 python3-venv ffmpeg git
```

```bash
sudo useradd --system --create-home --home-dir /var/lib/spankvanc --shell /usr/sbin/nologin spankvanc
```

Clone the repository (or branch) that contains the caption TCP output:

```bash
sudo git clone YOUR-SPANKVANC-REPOSITORY-URL /opt/SpankVANC
```

The application itself has no Python package dependencies.

## 3. Install Whisper in its own environment

```bash
sudo python3 -m venv /opt/whisper-venv
```

```bash
sudo /opt/whisper-venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
```

```bash
sudo /opt/whisper-venv/bin/pip install openai-whisper numpy
```

With an NVIDIA GPU, install the CUDA build of PyTorch instead of the `/whl/cpu`
one. See [NVIDIA GPU for caption generation](#nvidia-gpu-for-caption-generation).
Then create `/etc/spankvanc.env` (owner root, mode 600):

```
SPANKVANC_WHISPER_PYTHON=/opt/whisper-venv/bin/python
SPANKVANC_WHISPER_MODEL_CACHE=/var/lib/spankvanc/whisper-models
# Only for LAN access (step 5, option B); at least 12 characters:
# SPANKVANC_PASSWORD=change-this-long-password
```

Download the model once before going on air, so the first route start does not wait for it:

```bash
sudo -u spankvanc env SPANKVANC_WHISPER_MODEL_CACHE=/var/lib/spankvanc/whisper-models /opt/whisper-venv/bin/python -c "import whisper; whisper.load_model('small.en', download_root='/var/lib/spankvanc/whisper-models')"
```

## 4. Run as a service

```bash
sudo cp /opt/SpankVANC/examples/spankvanc.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now spankvanc
```

```bash
journalctl -u spankvanc -f
```

For a quick manual test, run `python3 -m spankvanc serve` from `/opt/SpankVANC` instead.

## 5. Open the control panel

**Option A, recommended: SSH tunnel.** The service stays on loopback. From your workstation:

```bash
ssh -L 8877:127.0.0.1:8877 user@vm-address
```

Then browse to `http://127.0.0.1:8877`.

**Option B: LAN access.** Set `SPANKVANC_PASSWORD` in `/etc/spankvanc.env` and use the
second `ExecStart` line in the unit:

```
--host 0.0.0.0 --allow-host 192.0.2.50
```

`--allow-host` lists every name or IP you will type into the browser. It is
repeatable, and requests with any other `Host` header are refused, which blocks
DNS rebinding. The browser asks for a password; any user name works. The service
refuses to bind beyond loopback without a password of at least 12 characters.
This is plain HTTP: the password and caption text are not encrypted. Use it only
on a trusted management VLAN or VPN, otherwise use the SSH tunnel.

## 6. Configure the route

1. **Source:** choose UDP (for example `udp://0.0.0.0:5000`, or `udp://@239.1.1.1:5000`
   for multicast), SRT, RTMP or a file played at program speed.
2. **Output type:** choose **Caption text · TCP / Telnet**. Captions are fixed
   to audioWhisper and SCTE to Strip, because this output carries text only.
3. **Caption encoder address**, in one of two modes:
   - `tcp://ENCODER-IP:PORT` (**client**). SpankVANC connects to the
     encoder's Telnet/raw-text port, reconnects with backoff (1–10 s) if it
     drops, and resends the phrase that was being sent when it failed.
   - `tcp://0.0.0.0:PORT?listen=1` (**listener**). Encoders or `telnet` clients
     connect to the VM. Up to eight clients receive the same text. Open the port in
     the VM firewall, for example `sudo ufw allow from 192.0.2.10 to any port 2001 proto tcp`.
4. **Line ending:** CR + LF suits most Telnet text ports. Use CR only or LF only
   if the encoder expects it. Tick **Send UPPERCASE text** for all-caps house style.
5. **Whisper:** choose the model, language, audio channel and device.
   **Auto** uses the NVIDIA GPU when CUDA works and falls back to CPU otherwise.
   **GPU · CUDA required** refuses to start without a working GPU.
6. **Check readiness**, then **Start**. The output card shows connection state,
   connected clients, lines sent/dropped and the last line sent.

You can add normal UDP/SRT/RTMP outputs to the same route; they are encoded by
FFmpeg as usual while the TCP output carries the caption text.

[`examples/vm-caption-tcp-route.json`](../examples/vm-caption-tcp-route.json)
is a complete route. Check it with `python3 -m spankvanc plan examples/vm-caption-tcp-route.json`.

### What is sent

- One Whisper phrase per line, printable ASCII only. Smart quotes and dashes are
  simplified, accents are removed, and other characters become `?`. Line breaks
  and control characters inside a phrase cannot reach the encoder.
- Nothing is sent for silence. The encoder's own roll-up/timeout behavior clears
  the display.
- Anything the encoder sends back (prompts, Telnet negotiation) is read and
  discarded. Nothing from the network is interpreted as a command.
- If the encoder is unreachable or the send queue is full, the oldest unsent phrases
  are dropped (up to 64 are held) to keep captions live, as with SDI embedding. The
  Whisper worker also drops a phrase rather than fall more than about eight seconds behind.

### Bench test without an encoder

Client mode: listen on another machine, then point the output at it:

```bash
nc -lk 2001
```

Listener mode: connect to the VM and watch the lines arrive:

```bash
telnet vm-address 2001
```

## NVIDIA GPU for caption generation

Whisper runs on an NVIDIA GPU through CUDA (fp16). A GPU lets larger, more
accurate models (`medium.en`, `turbo`, `large-v3`) keep up live; on CPU, use
`small.en` or `base.en`. Only caption recognition uses the GPU. Video for IP
outputs is still encoded on the CPU.

### 1. Pass the card through in Proxmox

1. Enable IOMMU in the host BIOS (VT-d / AMD-Vi) and on the kernel command line
   (`intel_iommu=on` on Intel; AMD enables it by default), then reboot the host.
2. Bind the card to `vfio-pci` on the host so the host driver does not claim it.
   Blacklist `nouveau`/`nvidia` on the host, or use a `vfio-pci ids=` option.
3. VM settings: machine type **q35**, BIOS **OVMF (UEFI)**, CPU type **host**.
   Under **Hardware → Add → PCI Device**, select the GPU with **All Functions**
   and **PCI-Express** enabled.

Follow the Proxmox *PCI Passthrough* documentation for your host version.
vGPU/mdev profiles also work if the guest sees a CUDA device.

### 2. Install the driver in the VM

```bash
sudo apt install -y ubuntu-drivers-common && sudo ubuntu-drivers install --gpgpu
```

```bash
sudo reboot
```

After the reboot, `nvidia-smi` must list the card. If it does not, the
problem is the passthrough or driver, not SpankVANC.

### 3. Install CUDA PyTorch for Whisper

```bash
sudo /opt/whisper-venv/bin/pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu124
```

Choose the `cuXXX` index matching the newest CUDA version that `nvidia-smi`
reports or lower. Check:

```bash
/opt/whisper-venv/bin/python -c "import torch; print(torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 4. Use it

Press **Refresh devices** (or restart the service). **Whisper CUDA** should show
as available, and **Check readiness** names the GPU that will be used. When
CUDA is unavailable, readiness says why: a CPU-only PyTorch build, a driver or
passthrough problem, or a CUDA initialisation error. When the route starts,
the engine log shows `Whisper ready: <model> / cuda (<GPU name>)`.

With several GPUs, choose one by adding `CUDA_VISIBLE_DEVICES=1` to
`/etc/spankvanc.env`. The GPU can also be shared with other VMs only through
vGPU, not plain passthrough.

## Limits and verification

- Software-verified: unit tests cover address parsing, text formatting,
  client reconnect, listener broadcast, Telnet input being ignored, LAN
  password/Host checks and preflight. An end-to-end route with Ubuntu's stock
  FFmpeg and a stub recognizer delivered every phrase over TCP.
- **Not yet verified against a physical caption encoder.** Encoder text-port
  conventions vary, and some expect a login, special control codes or a specific
  line ending. Confirm with the encoder's manual and a bench test before air.
- Spanish CC2 translation is not available on the TCP output.
- GPU detection and device selection are software-tested, including against a real
  CPU-only PyTorch build. Recognition on a passed-through GPU inside Proxmox has
  not been run as part of this work.
- Recognition latency (about three seconds of audio per phrase plus processing)
  is not compensated. Program A/V is not delayed.
- DeckLink inside a VM needs PCIe passthrough of the card, Desktop Video for
  Linux in the guest, and the local engine build in [UBUNTU_DECKLINK.md](UBUNTU_DECKLINK.md).
  That path is untested in a VM.
