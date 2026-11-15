#!/usr/bin/env bash
# S01 preflight: read-only checks. Installs nothing, never uses sudo.
# Output: table of OK/MISSING, then sudo commands and firewall text for the owner.
set -u

MIN_DRIVER=570
MIN_VRAM_MIB=3500      # chat model + KV cache need about 4 GB
MIN_RAM_MIB=5000       # "available" RAM, not "free"
PORTS="3000 9090 9835 8080 11434"   # Grafana, Prometheus, GPU exporter, proxy/agent port-forward, Ollama
TOOLS="kind kubectl helm gitleaks uv node ollama podman exiftool"
OPTIONAL="exiftool"

rows=(); missing=()
row() { rows+=("$(printf '%-34s %-8s %s' "$1" "$2" "$3")"); }

# --- NVIDIA driver and VRAM
if command -v nvidia-smi >/dev/null; then
  IFS=',' read -r drv free <<<"$(nvidia-smi --query-gpu=driver_version,memory.free --format=csv,noheader,nounits | head -1)"
  drv=${drv// /}; free=${free// /}
  [ "${drv%%.*}" -ge "$MIN_DRIVER" ] && s=OK || s=MISSING
  row "NVIDIA driver >= $MIN_DRIVER" "$s" "found $drv"
  [ "$free" -ge "$MIN_VRAM_MIB" ] && s=OK || s=MISSING
  row "Free VRAM >= $MIN_VRAM_MIB MiB" "$s" "free ${free} MiB"
else
  row "NVIDIA driver >= $MIN_DRIVER" MISSING "nvidia-smi not found"
  row "Free VRAM >= $MIN_VRAM_MIB MiB" MISSING "no GPU info"
fi

# --- RAM
avail=$(free -m | awk '/^Mem:/{print $7}')
[ "$avail" -ge "$MIN_RAM_MIB" ] && s=OK || s=MISSING
row "Available RAM >= $MIN_RAM_MIB MiB" "$s" "available ${avail} MiB"

# --- Ports (in use by ollama on 11434 is fine: that is our LLM)
for p in $PORTS; do
  line=$(ss -ltnpH "sport = :$p" 2>/dev/null | head -1)
  if [ -z "$line" ]; then s=OK; n="free"
  elif [ "$p" = 11434 ] && curl -s -m 2 "http://127.0.0.1:$p/" | grep -q "Ollama is running"; then s=OK; n="used by ollama (expected)"
  else s=MISSING; n="in use"; fi
  row "Port $p" "$s" "$n"
done

# --- podman rootless + cgroup v2 delegation
if command -v podman >/dev/null; then
  [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] && s=OK || s=MISSING
  row "podman rootless" "$s" ""
  [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] && s=OK || s=MISSING
  row "cgroup v2" "$s" ""
  deleg=$(cat "/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers" 2>/dev/null)
  ok=OK; for c in cpu memory pids; do grep -qw "$c" <<<"$deleg" || ok=MISSING; done
  row "cgroup delegation (cpu memory pids)" "$ok" "${deleg:-none}"
else
  row "podman rootless" MISSING "podman not installed"
fi

# --- Tools
for t in $TOOLS; do
  if command -v "$t" >/dev/null; then s=OK; n="$(command -v "$t")"
  else
    s=MISSING; n=""
    case " $OPTIONAL " in *" $t "*) n="(optional)";; *) ;; esac
    missing+=("$t")
  fi
  row "Tool $t" "$s" "$n"
done

printf '%-34s %-8s %s\n' ITEM STATUS NOTE
printf '%s\n' "${rows[@]}"

# --- Install commands: repo package name from pacman -Si (read-only)
echo
pkgs=()
for t in "${missing[@]}"; do
  # package name equals tool name for all tools here except none; verify in the repo
  q=$t; [ "$t" = exiftool ] && q=perl-image-exiftool   # Arch name differs from the tool name
  if pkg=$(pacman -Si "$q" 2>/dev/null | awk -F': *' '/^Name/{print $2; exit}') && [ -n "$pkg" ]; then
    repo=$(pacman -Si "$q" | awk -F': *' '/^Repository/{print $2; exit}')
    echo "pacman -Si $q -> package '$pkg' in repo '$repo'"
    pkgs+=("$pkg")
  else
    echo "pacman -Si $q -> not in repos (check: paru -Si $q)"
  fi
done
echo
if [ "${#pkgs[@]}" -gt 0 ]; then
  echo "Run this yourself (the owner). The script never installs:"
  echo "  sudo pacman -S --needed ${pkgs[*]}"
  echo "(exiftool is optional: drop perl-image-exiftool if you do not want it; PIL is the default)"
fi

cat <<'TXT'

Firewall rule for the Ollama port (needed in S12; the owner runs it).
Goal: only the local kind network may reach 11434. Adjust the interface/subnet after S03/S12
(find it with: podman network inspect kind | grep -i subnet). ufw is installed here:
  sudo ufw allow from <kind-subnet> to any port 11434 proto tcp
  (if ufw is inactive, the rule has no effect; check with: sudo ufw status)
Not wanted: any rule that opens 11434 to the LAN or the internet.
TXT
