#!/usr/bin/env bash
# Create the GitHub repo and push (run once).
set -e
cd "$(dirname "$0")"
gh repo create yamuna-flood-mapper --public --source=. --push \
  --description "Sentinel-1 SAR flood mapping for the Yamuna corridor, Delhi — free data, free hosting" || {
  echo "repo may already exist — adding remote"; git remote add origin \
  "https://github.com/saketkumar-18/yamuna-flood-mapper.git" 2>/dev/null || true; git push -u origin master || git push -u origin main; }
echo done
