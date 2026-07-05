FROM python:3.12-alpine

# Install GitHub CLI and CA certificates so the tracker can call `gh api`
# and the Devin API without running locally.
RUN apk add --no-cache github-cli py3-certifi

WORKDIR /app

COPY playbook.md sync_playbook.py sessions.py tracker.py ./
COPY knowledge ./knowledge/

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python3 sync_playbook.py && python3 tracker.py"]
