#!/usr/bin/env python3
"""Install the pinned official Codex CLI privately; no npm/root/API key needed."""
import base64
import hashlib
import json
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION='0.162.1'
def main():
    if platform.system()!='Linux':raise SystemExit('This installer targets Linux only')
    arch={'x86_64':'x64','aarch64':'arm64'}.get(platform.machine())
    if arch is None:raise SystemExit('Unsupported architecture')
    destination=Path.home()/'.local/share/codex-local-bridge/engine-cli'/VERSION
    binary=destination/'bin/codex'
    if binary.is_file():print(binary);return
    url='https://registry.npmjs.org/@openai/codex/'+VERSION+'-linux-'+arch
    with urllib.request.urlopen(url,timeout=60) as response:metadata=json.load(response)
    dist=metadata['dist']
    if not dist['tarball'].startswith('https://registry.npmjs.org/'):raise ValueError('Unexpected package origin')
    with tempfile.TemporaryDirectory(prefix='bridge-engine-') as scratch:
        archive=Path(scratch)/'package.tgz'
        with urllib.request.urlopen(dist['tarball'],timeout=60) as response,archive.open('wb') as out:
            while chunk:=response.read(1024*1024):out.write(chunk)
        algorithm,expected=dist['integrity'].split('-',1)
        h=hashlib.new(algorithm)
        with archive.open('rb') as inp:
            while chunk:=inp.read(1024*1024):h.update(chunk)
        if base64.b64encode(h.digest()).decode()!=expected:raise ValueError('Package integrity mismatch')
        # Copy specific regular files only; never extract arbitrary archive paths.
        with tarfile.open(archive) as package:
            for suffix in ('bin/codex-code-mode-host','codex-resources/bwrap','bin/codex'):
                member=next(m for m in package if m.isfile() and m.name.endswith('/'+suffix))
                target=destination/suffix;target.parent.mkdir(parents=True,exist_ok=True)
                data=package.extractfile(member)
                tmp=target.with_suffix('.installing')
                with tmp.open('wb') as out:
                    while chunk:=data.read(1024*1024):out.write(chunk)
                tmp.chmod(0o700);tmp.replace(target)
    print(binary)

if __name__=='__main__':main()
