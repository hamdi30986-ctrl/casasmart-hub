#!/usr/bin/env bash
# CasaSmart Hub release script — the only supported way to cut a release.
#
# HACS installs a release by its TAG and extracts the release asset named in
# hacs.json (casasmart.zip) into custom_components/casasmart; Home Assistant and
# the hub handshake report manifest.json's "version". These must never disagree,
# so every step below refuses to continue on any mismatch.
#
#   scripts/release.sh check   X.Y.Z   gates only (clean main, manifest, tests); no changes
#   scripts/release.sh tag     X.Y.Z   gates + annotated tag + build/sign/verify dist/ (local)
#   scripts/release.sh publish X.Y.Z   push the tag + create the GitHub release as a PRERELEASE
#   scripts/release.sh promote X.Y.Z   mark the prerelease as the latest release (same tag/asset)
#
# Tags and published assets are never moved or replaced: fix forward with X.Y.Z+1.
set -euo pipefail

cmd="${1:-}"
version="${2:-}"
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

manifest="custom_components/casasmart/manifest.json"
signing_key="${CASASMART_RELEASE_KEY:-$HOME/.casasmart/release_ed25519.pem}"
dist="$repo_root/dist"
tag="v$version"

die() { echo "release: $*" >&2; exit 1; }
step() { echo "==> $*"; }

[[ "$cmd" =~ ^(check|tag|publish|promote)$ ]] || die "usage: $0 {check|tag|publish|promote} X.Y.Z"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be three-part X.Y.Z (got '$version')"

run_python() {
  if command -v uv >/dev/null 2>&1; then
    uv run --quiet --python 3.13 --no-project --with-requirements requirements_test.txt -- python "$@"
  else
    python3 "$@"
  fi
}

gates() {
  step "gates for $tag"
  [ -z "$(git status --porcelain)" ] || die "working tree is not clean"
  # Any branch or worktree may release, as long as HEAD is exactly what is on
  # GitHub's main.
  git fetch --quiet origin main --tags
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "HEAD is not origin/main (push it first: git push origin HEAD:main)"
  local mv
  mv="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$manifest")"
  [ "$mv" = "$version" ] || die "manifest.json version is $mv, expected $version"
  grep -q "^## \[$version\]" CHANGELOG.md || die "CHANGELOG.md has no '## [$version]' section"
  step "tests"
  run_python -m pytest -q
}

build_and_verify() {
  step "build $dist/casasmart.zip from $tag"
  rm -rf "$dist" && mkdir -p "$dist"
  # Files at the zip root, exactly the tagged integration tree (reproduces v2.1's asset).
  git archive --format=zip -o "$dist/casasmart.zip" "$tag:custom_components/casasmart"
  local zv
  zv="$(unzip -p "$dist/casasmart.zip" manifest.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')"
  [ "$zv" = "$version" ] || die "zip manifest.json says $zv"
  [ -f "$signing_key" ] || die "release signing key not found at $signing_key"
  step "sign"
  openssl pkeyutl -sign -rawin -inkey "$signing_key" -in "$dist/casasmart.zip" -out "$dist/casasmart.zip.sig"
  step "verify signature against the key pinned in the integration"
  run_python - "$dist/casasmart.zip" "$dist/casasmart.zip.sig" <<'PY'
import base64, re, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
const = Path("custom_components/casasmart/const.py").read_text()
m = re.search(r'UPDATE_SIGNING_PUBLIC_KEY_B64\s*=\s*"([^"]+)"', const)
if not m:
    sys.exit("pinned UPDATE_SIGNING_PUBLIC_KEY_B64 not found in const.py")
key = Ed25519PublicKey.from_public_bytes(base64.b64decode(m.group(1)))
key.verify(Path(sys.argv[2]).read_bytes(), Path(sys.argv[1]).read_bytes())
print("signature OK")
PY
  awk -v v="$version" '$0 ~ "^## \\[" v "\\]" {f=1; next} /^## \[/ {f=0} f' CHANGELOG.md > "$dist/notes.md"
  [ -s "$dist/notes.md" ] || die "empty release notes for $version"
  (cd "$dist" && shasum -a 256 casasmart.zip casasmart.zip.sig)
}

case "$cmd" in
  check)
    gates
    step "OK: $tag is releasable"
    ;;
  tag)
    gates
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
    git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1 && die "tag $tag already exists on origin"
    git tag -a "$tag" -m "CasaSmart Hub $tag"
    build_and_verify
    step "OK: $tag tagged locally and dist/ built — next: $0 publish $version"
    ;;
  publish)
    [ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse origin/main)" ] || die "$tag is not origin/main"
    [ -f "$dist/casasmart.zip" ] && [ -f "$dist/casasmart.zip.sig" ] || die "run '$0 tag $version' first"
    git push origin "$tag"
    gh release create "$tag" "$dist/casasmart.zip" "$dist/casasmart.zip.sig" \
      --verify-tag --prerelease --title "CasaSmart Hub $tag" --notes-file "$dist/notes.md"
    step "OK: $tag published as a PRERELEASE — verify it, then: $0 promote $version"
    ;;
  promote)
    gh release edit "$tag" --prerelease=false --latest
    latest="$(gh api "repos/{owner}/{repo}/releases/latest" --jq .tag_name)"
    [ "$latest" = "$tag" ] || die "latest release is $latest, expected $tag"
    step "OK: $tag is the latest release"
    ;;
esac
