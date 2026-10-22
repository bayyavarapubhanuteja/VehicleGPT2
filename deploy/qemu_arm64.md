# Running on emulated ARM (QEMU) — Cortex-A53/A55/A72 class, 2 vCPU, 2 GB

On any Linux/macOS host with QEMU and Docker buildx:
```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64      # register QEMU user-mode emulation
docker buildx build --platform linux/arm64 -t vehiclegpt:edge -f deploy/Dockerfile --load .
deploy/run_edge.sh profile                                           # 2 cores, 2 GB, --network none
```
Full-system emulation (closer to a real board; slower):
```bash
qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 2 -m 2048 -nographic \
  -drive if=virtio,file=debian-12-arm64.qcow2 -nic user,hostfwd=tcp::8502-:8502
# inside the guest: git clone / copy the project, then deploy/pi_setup.sh
```
Note: user-mode QEMU emulation is 5–20× slower than native ARM; use it to prove the build and memory budget, and a real
Pi 5 / Graviton instance for latency numbers.
