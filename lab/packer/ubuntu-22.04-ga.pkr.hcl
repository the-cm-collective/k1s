packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.1.0"
    }
  }
}

variable "variant" {
  type    = string
  default = "base"
  validation {
    condition     = contains(["base", "gpu"], var.variant)
    error_message = "Variant must be base or gpu."
  }
}

variable "output_dir" {
  type    = string
  default = "artifacts/images"
}

variable "seed_bundle" {
  type = string
}

variable "vm_memory" {
  type    = number
  default = 4096
}

variable "vm_cpus" {
  type    = number
  default = 2
}

variable "ubuntu_image_url" {
  type    = string
  default = "https://cloud-images.ubuntu.com/releases/jammy/release/ubuntu-22.04-server-cloudimg-amd64.img"
}

variable "ubuntu_image_checksum" {
  type    = string
  default = "sha256:e66ef756881b5e682c496112201382abd76291797a7395bf81fd1bd0888f5b6f"
}

source "qemu" "ubuntu" {
  accelerator      = "kvm"
  communicator     = "ssh"
  disk_interface   = "virtio"
  disk_image       = true
  format           = "qcow2"
  headless         = true
  iso_checksum     = var.ubuntu_image_checksum
  iso_url          = var.ubuntu_image_url
  memory           = var.vm_memory
  cpus             = var.vm_cpus
  output_directory = "${var.output_dir}/build-${var.variant}"
  shutdown_command = "echo 'packer' | sudo -S shutdown -P now"
  ssh_username     = "packer"
  ssh_password     = "packer"
  ssh_timeout      = "25m"
  vm_name          = "ubuntu-22.04-${var.variant}.qcow2"

  cd_files = [
    "lab/packer/http/user-data",
    "lab/packer/http/meta-data",
  ]
  cd_label = "cidata"

  qemuargs = [
    ["-cpu", "host"],
    ["-serial", "stdio"],
  ]
}

build {
  name    = "ubuntu-22.04-ga"
  sources = ["source.qemu.ubuntu"]

  provisioner "file" {
    source      = "lab/packer/http/common-bootstrap.sh"
    destination = "/tmp/common-bootstrap.sh"
  }

  provisioner "file" {
    source      = "lab/packer/http/gpu-bootstrap.sh"
    destination = "/tmp/gpu-bootstrap.sh"
  }

  provisioner "file" {
    source      = "lab/variants/cri_seed_images.lock.json"
    destination = "/tmp/cri_seed_images.lock.json"
  }

  provisioner "file" {
    source      = var.seed_bundle
    destination = "/tmp/cri-seed-images.oci.tar"
  }

  provisioner "file" {
    source      = "scripts/cri_smoke.sh"
    destination = "/tmp/cri_smoke.sh"
  }

  provisioner "shell" {
    execute_command = "echo 'packer' | {{.Vars}} sudo -S -E bash -euo pipefail '{{.Path}}'"
    inline = [
      "chmod +x /tmp/common-bootstrap.sh /tmp/gpu-bootstrap.sh /tmp/cri_smoke.sh",
      "/tmp/common-bootstrap.sh ${var.variant} /tmp/cri_seed_images.lock.json /tmp/cri-seed-images.oci.tar /tmp/cri_smoke.sh",
      "if [ '${var.variant}' = 'gpu' ]; then echo '[image-bootstrap] running gpu bootstrap'; /tmp/gpu-bootstrap.sh; echo '[image-bootstrap] gpu bootstrap complete'; fi",
      "cloud-init clean --logs",
      "truncate -s 0 /etc/machine-id",
      "rm -f /var/lib/dbus/machine-id",
    ]
  }

  post-processor "shell-local" {
    inline = [
      "mkdir -p ${var.output_dir}",
      "cp ${var.output_dir}/build-${var.variant}/ubuntu-22.04-${var.variant}.qcow2 ${var.output_dir}/ubuntu-22.04-k1s-${var.variant}.qcow2",
    ]
  }

  post-processor "manifest" {
    output = "${var.output_dir}/manifest-${var.variant}.json"
    custom_data = {
      distro  = "ubuntu"
      release = "22.04"
      kernel  = "ga-5.15"
      variant = "${var.variant}"
    }
  }
}
