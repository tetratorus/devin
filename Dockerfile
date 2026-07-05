FROM python:3.12-alpine

# Install GitHub CLI so the tracker can call `gh api` without running locally.
# python:3.12-alpine already enables the community repo, which contains github-cli.
RUN apk add --no-cache github-cli

WORKDIR /app

COPY tracker.py sessions.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python3", "tracker.py"]
