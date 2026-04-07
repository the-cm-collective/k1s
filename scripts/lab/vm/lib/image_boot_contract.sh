#!/usr/bin/env bash

boot_contract_reset() {
  BOOT_CONTRACT_IMAGE_PATH=""
  BOOT_CONTRACT_NBD_DEVICE=""
  BOOT_CONTRACT_MOUNT_DIR=""
  BOOT_CONTRACT_ROOT_DIR=""
  BOOT_CONTRACT_ROOT_PART=""
  BOOT_CONTRACT_ROOT_UUID=""
  BOOT_CONTRACT_ROOT_LABEL=""
  BOOT_CONTRACT_LSBLK_OUTPUT=""
  BOOT_CONTRACT_BLKID_OUTPUT=""
  BOOT_CONTRACT_FIXTURE=0
}

boot_contract_reset

boot_contract_err() {
  printf '[image-boot-contract] %s\n' "$*" >&2
}

boot_contract_require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    boot_contract_err "required command missing: ${cmd}"
    return 1
  }
}

boot_contract_file_cat() {
  local rel_path="$1"
  if [[ "$BOOT_CONTRACT_FIXTURE" -eq 1 ]]; then
    cat "${BOOT_CONTRACT_ROOT_DIR}${rel_path}"
    return 0
  fi
  sudo cat "${BOOT_CONTRACT_ROOT_DIR}${rel_path}"
}

boot_contract_file_readlink() {
  local rel_path="$1"
  if [[ "$BOOT_CONTRACT_FIXTURE" -eq 1 ]]; then
    readlink -f "${BOOT_CONTRACT_ROOT_DIR}${rel_path}"
    return 0
  fi
  sudo readlink -f "${BOOT_CONTRACT_ROOT_DIR}${rel_path}"
}

boot_contract_pick_root_part() {
  local line="" part="" part_type=""
  while IFS= read -r line; do
    [[ "$line" =~ ^([^:]+): ]] || continue
    part="${BASH_REMATCH[1]}"
    [[ "$line" =~ TYPE=\"([^\"]+)\" ]] || continue
    part_type="${BASH_REMATCH[1]}"
    case "$part_type" in
      ext2|ext3|ext4|xfs|btrfs)
        printf '%s\n' "$part"
        return 0
        ;;
    esac
  done <<<"$BOOT_CONTRACT_BLKID_OUTPUT"
  return 1
}

boot_contract_lookup_uuid() {
  local target_part="$1"
  local line="" part="" uuid=""
  while IFS= read -r line; do
    [[ "$line" =~ ^([^:]+): ]] || continue
    part="${BASH_REMATCH[1]}"
    [[ "$part" == "$target_part" ]] || continue
    [[ "$line" =~ UUID=\"([^\"]+)\" ]] || continue
    uuid="${BASH_REMATCH[1]}"
    printf '%s\n' "$uuid"
    return 0
  done <<<"$BOOT_CONTRACT_BLKID_OUTPUT"
  return 1
}

boot_contract_lookup_label() {
  local target_part="$1"
  local line="" part="" label=""
  while IFS= read -r line; do
    [[ "$line" =~ ^([^:]+): ]] || continue
    part="${BASH_REMATCH[1]}"
    [[ "$part" == "$target_part" ]] || continue
    [[ "$line" =~ LABEL=\"([^\"]+)\" ]] || continue
    label="${BASH_REMATCH[1]}"
    printf '%s\n' "$label"
    return 0
  done <<<"$BOOT_CONTRACT_BLKID_OUTPUT"
  return 1
}

boot_contract_fstab_root_source() {
  boot_contract_file_cat /etc/fstab 2>/dev/null | awk '
    /^[[:space:]]*#/ { next }
    $2 == "/" { print $1; exit }
  '
}

boot_contract_fstab_root_uuid() {
  local source=""
  source="$(boot_contract_fstab_root_source)"
  if [[ "$source" == UUID=* ]]; then
    printf '%s\n' "${source#UUID=}"
  fi
}

boot_contract_fstab_root_label() {
  local source=""
  source="$(boot_contract_fstab_root_source)"
  if [[ "$source" == LABEL=* ]]; then
    printf '%s\n' "${source#LABEL=}"
  fi
}

boot_contract_grub_root_uuids() {
  boot_contract_file_cat /boot/grub/grub.cfg 2>/dev/null |
    grep -oE 'root=UUID=[^"[:space:]]+' |
    sed 's/root=UUID=//' |
    awk 'NF && !seen[$0]++ { print $0 }'
}

boot_contract_grub_root_lines() {
  boot_contract_file_cat /boot/grub/grub.cfg 2>/dev/null |
    grep -nE 'root=UUID=|search[.]fs_uuid' || true
}

boot_contract_find_free_nbd() {
  local sysdev="" size=""
  for sysdev in /sys/class/block/nbd*; do
    [[ -e "$sysdev" ]] || continue
    size="$(cat "$sysdev/size" 2>/dev/null || printf '1')"
    if [[ "$size" == "0" ]]; then
      printf '/dev/%s\n' "$(basename "$sysdev")"
      return 0
    fi
  done
  boot_contract_err "no free /dev/nbd device available"
  return 1
}

boot_contract_cleanup() {
  if [[ "$BOOT_CONTRACT_FIXTURE" -eq 0 && -n "$BOOT_CONTRACT_MOUNT_DIR" && -d "$BOOT_CONTRACT_MOUNT_DIR" ]]; then
    sudo umount "$BOOT_CONTRACT_MOUNT_DIR" >/dev/null 2>&1 || true
    rmdir "$BOOT_CONTRACT_MOUNT_DIR" >/dev/null 2>&1 || true
  fi
  if [[ "$BOOT_CONTRACT_FIXTURE" -eq 0 && -n "$BOOT_CONTRACT_NBD_DEVICE" ]]; then
    sudo qemu-nbd --disconnect "$BOOT_CONTRACT_NBD_DEVICE" >/dev/null 2>&1 || true
  fi
  boot_contract_reset
}

boot_contract_use_fixture() {
  local fixture_dir="$1"
  [[ -d "$fixture_dir" ]] || {
    boot_contract_err "fixture dir missing: ${fixture_dir}"
    return 1
  }
  [[ -f "$fixture_dir/lsblk.txt" ]] || {
    boot_contract_err "fixture lsblk.txt missing: ${fixture_dir}/lsblk.txt"
    return 1
  }
  [[ -f "$fixture_dir/blkid.txt" ]] || {
    boot_contract_err "fixture blkid.txt missing: ${fixture_dir}/blkid.txt"
    return 1
  }
  [[ -d "$fixture_dir/rootfs" ]] || {
    boot_contract_err "fixture rootfs missing: ${fixture_dir}/rootfs"
    return 1
  }

  BOOT_CONTRACT_FIXTURE=1
  BOOT_CONTRACT_LSBLK_OUTPUT="$(cat "$fixture_dir/lsblk.txt")"
  BOOT_CONTRACT_BLKID_OUTPUT="$(cat "$fixture_dir/blkid.txt")"
  BOOT_CONTRACT_ROOT_DIR="$fixture_dir/rootfs"
  BOOT_CONTRACT_ROOT_PART="$(boot_contract_pick_root_part)"
  [[ -n "$BOOT_CONTRACT_ROOT_PART" ]] || {
    boot_contract_err "unable to identify root partition from fixture blkid data"
    return 1
  }
  BOOT_CONTRACT_ROOT_UUID="$(boot_contract_lookup_uuid "$BOOT_CONTRACT_ROOT_PART")"
  [[ -n "$BOOT_CONTRACT_ROOT_UUID" ]] || {
    boot_contract_err "unable to determine root UUID for ${BOOT_CONTRACT_ROOT_PART}"
    return 1
  }
  BOOT_CONTRACT_ROOT_LABEL="$(boot_contract_lookup_label "$BOOT_CONTRACT_ROOT_PART" || true)"
}

boot_contract_open_image() {
  local image="$1"
  local fixture_dir="${IMAGE_BOOT_CONTRACT_FIXTURE_DIR:-}"
  local device_rows="" nbd_device="" root_part=""

  boot_contract_reset
  BOOT_CONTRACT_IMAGE_PATH="$image"

  if [[ -n "$fixture_dir" ]]; then
    boot_contract_use_fixture "$fixture_dir"
    return 0
  fi

  [[ -f "$image" ]] || {
    boot_contract_err "image not found: ${image}"
    return 1
  }

  boot_contract_require_cmd qemu-nbd
  boot_contract_require_cmd lsblk
  boot_contract_require_cmd blkid
  boot_contract_require_cmd mount
  boot_contract_require_cmd umount

  sudo modprobe nbd max_part=16 >/dev/null 2>&1 || true

  if [[ -n "${IMAGE_BOOT_CONTRACT_NBD_DEVICE:-}" ]]; then
    nbd_device="${IMAGE_BOOT_CONTRACT_NBD_DEVICE}"
  else
    nbd_device="$(boot_contract_find_free_nbd)"
  fi

  BOOT_CONTRACT_NBD_DEVICE="$nbd_device"
  sudo qemu-nbd --read-only --connect="$BOOT_CONTRACT_NBD_DEVICE" "$image"
  if command -v udevadm >/dev/null 2>&1; then
    sudo udevadm settle >/dev/null 2>&1 || true
  fi

  BOOT_CONTRACT_LSBLK_OUTPUT="$(sudo lsblk -o NAME,SIZE,FSTYPE,UUID,PARTUUID,MOUNTPOINT "$BOOT_CONTRACT_NBD_DEVICE")"
  device_rows="$(sudo lsblk -nrpo NAME "$BOOT_CONTRACT_NBD_DEVICE")"
  BOOT_CONTRACT_BLKID_OUTPUT="$(sudo blkid $device_rows 2>/dev/null || true)"
  root_part="$(boot_contract_pick_root_part)"
  [[ -n "$root_part" ]] || {
    boot_contract_err "unable to identify root partition for ${image}"
    return 1
  }

  BOOT_CONTRACT_ROOT_PART="$root_part"
  BOOT_CONTRACT_ROOT_UUID="$(boot_contract_lookup_uuid "$BOOT_CONTRACT_ROOT_PART")"
  [[ -n "$BOOT_CONTRACT_ROOT_UUID" ]] || {
    boot_contract_err "unable to determine root UUID for ${BOOT_CONTRACT_ROOT_PART}"
    return 1
  }
  BOOT_CONTRACT_ROOT_LABEL="$(boot_contract_lookup_label "$BOOT_CONTRACT_ROOT_PART" || true)"

  BOOT_CONTRACT_MOUNT_DIR="$(mktemp -d)"
  sudo mount -o ro "$BOOT_CONTRACT_ROOT_PART" "$BOOT_CONTRACT_MOUNT_DIR"
  BOOT_CONTRACT_ROOT_DIR="$BOOT_CONTRACT_MOUNT_DIR"
}

boot_contract_print_report() {
  local fstab_root_source="" fstab_root_uuid="" fstab_root_label="" grub_root_uuids=""
  local vmlinuz_target="" initrd_target=""
  fstab_root_source="$(boot_contract_fstab_root_source)"
  fstab_root_uuid="$(boot_contract_fstab_root_uuid)"
  fstab_root_label="$(boot_contract_fstab_root_label)"
  grub_root_uuids="$(boot_contract_grub_root_uuids | paste -sd',' -)"
  vmlinuz_target="$(boot_contract_file_readlink /boot/vmlinuz 2>/dev/null || true)"
  initrd_target="$(boot_contract_file_readlink /boot/initrd.img 2>/dev/null || true)"

  printf 'image=%s\n' "$BOOT_CONTRACT_IMAGE_PATH"
  printf 'nbd_device=%s\n' "${BOOT_CONTRACT_NBD_DEVICE:-fixture}"
  printf 'root_partition=%s\n' "$BOOT_CONTRACT_ROOT_PART"
  printf 'root_uuid=%s\n' "$BOOT_CONTRACT_ROOT_UUID"
  printf 'root_label=%s\n' "$BOOT_CONTRACT_ROOT_LABEL"
  printf 'fstab_root_source=%s\n' "$fstab_root_source"
  printf 'fstab_root_uuid=%s\n' "$fstab_root_uuid"
  printf 'fstab_root_label=%s\n' "$fstab_root_label"
  printf 'grub_root_uuids=%s\n' "$grub_root_uuids"
  printf '/boot/vmlinuz -> %s\n' "$vmlinuz_target"
  printf '/boot/initrd.img -> %s\n' "$initrd_target"
  printf -- '--- lsblk ---\n%s\n' "$BOOT_CONTRACT_LSBLK_OUTPUT"
  printf -- '--- blkid ---\n%s\n' "$BOOT_CONTRACT_BLKID_OUTPUT"
  printf -- '--- /etc/fstab ---\n'
  boot_contract_file_cat /etc/fstab 2>/dev/null || true
  printf -- '--- /boot/grub/grub.cfg (root lines) ---\n'
  boot_contract_grub_root_lines
}

boot_contract_assert() {
  local fstab_root_source="" fstab_root_uuid="" fstab_root_label="" grub_uuid="" failed=0
  local -a grub_root_uuids=()

  [[ -n "$BOOT_CONTRACT_ROOT_UUID" ]] || {
    boot_contract_err "root UUID missing"
    return 1
  }

  fstab_root_source="$(boot_contract_fstab_root_source)"
  if [[ -z "$fstab_root_source" ]]; then
    boot_contract_err "missing root filesystem entry in /etc/fstab"
    failed=1
  elif [[ "$fstab_root_source" == UUID=* ]]; then
    fstab_root_uuid="${fstab_root_source#UUID=}"
    if [[ "$fstab_root_uuid" != "$BOOT_CONTRACT_ROOT_UUID" ]]; then
      boot_contract_err "fstab root UUID mismatch: expected ${BOOT_CONTRACT_ROOT_UUID}, found ${fstab_root_uuid}"
      failed=1
    fi
  elif [[ "$fstab_root_source" == LABEL=* ]]; then
    fstab_root_label="${fstab_root_source#LABEL=}"
    if [[ -z "$BOOT_CONTRACT_ROOT_LABEL" ]]; then
      boot_contract_err "root filesystem label missing for ${BOOT_CONTRACT_ROOT_PART}"
      failed=1
    elif [[ "$fstab_root_label" != "$BOOT_CONTRACT_ROOT_LABEL" ]]; then
      boot_contract_err "fstab root LABEL mismatch: expected ${BOOT_CONTRACT_ROOT_LABEL}, found ${fstab_root_label}"
      failed=1
    fi
  else
    boot_contract_err "root filesystem entry in /etc/fstab must use UUID=... or LABEL=... (found: ${fstab_root_source})"
    failed=1
  fi

  mapfile -t grub_root_uuids < <(boot_contract_grub_root_uuids)
  if [[ "${#grub_root_uuids[@]}" -eq 0 ]]; then
    boot_contract_err "no root=UUID= entries found in /boot/grub/grub.cfg"
    failed=1
  else
    for grub_uuid in "${grub_root_uuids[@]}"; do
      if [[ "$grub_uuid" != "$BOOT_CONTRACT_ROOT_UUID" ]]; then
        boot_contract_err "grub root UUID mismatch: expected ${BOOT_CONTRACT_ROOT_UUID}, found ${grub_uuid}"
        failed=1
      fi
    done
  fi

  [[ "$failed" -eq 0 ]]
}
