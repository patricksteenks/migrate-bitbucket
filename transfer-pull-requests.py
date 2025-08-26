# === CONFIG ===
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv('tokens.env')
import json
import time
import os
import subprocess

BITBUCKET_USERNAME = os.environ['BITBUCKET_USERNAME']
BITBUCKET_APP_PASSWORD = os.environ['BITBUCKET_APP_PASSWORD']
GITHUB_TOKEN = os.environ['GITHUB_TOKEN']

BITBUCKET_REPO = 'trailblazersbv/tbws'
GITHUB_REPO = 'trailblazersbv/tbws'
GITHUB_PUSH_URL = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

TRACK_FILE = 'transferred_prs.json'
BITBUCKET_CACHE_FILE = 'bitbucket_prs.json'
CLONE_DIR = 'bitbucket_clone'

# === STATE TRACKING ===
EXCLUDE_FILE = 'exclude_prs.json'

if os.path.exists(EXCLUDE_FILE):
    with open(EXCLUDE_FILE, 'r') as f:
        exclude_prs = set(json.load(f))
else:
    exclude_prs = set()

if os.path.exists(TRACK_FILE):
    with open(TRACK_FILE, 'r') as f:
        transferred_prs = set(json.load(f))
else:
    transferred_prs = set()

def save_transferred(pr_id):
    transferred_prs.add(pr_id)
    with open(TRACK_FILE, 'w') as f:
        json.dump(list(transferred_prs), f)


# === FETCH PR DATA ===

def load_or_fetch_bitbucket_prs():
    if os.path.exists(BITBUCKET_CACHE_FILE):
        with open(BITBUCKET_CACHE_FILE, 'r') as f:
            return json.load(f)

    url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_REPO}/pullrequests?state=MERGED"
    prs = []

    while url:
        print("🔍 Fetching:", url)  # debug
        resp = requests.get(url, auth=(BITBUCKET_USERNAME, BITBUCKET_APP_PASSWORD))
        print("🔍 Status:", resp.status_code)
        print("🔍 Body:", resp.text[:500])  # show snippet
        resp.raise_for_status()

        data = resp.json()
        prs.extend(data.get("values", []))
        url = data.get("next")

    with open(BITBUCKET_CACHE_FILE, 'w') as f:
        json.dump(prs, f, indent=2)

    return prs


# === GIT OPERATIONS ===

def run_git(*args, cwd=None):
    result = subprocess.run(['git'] + list(args), cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Git command failed: {' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()

def ensure_repo_cloned():
    if not os.path.exists(CLONE_DIR):
        print(f"Cloning Bitbucket repo to {CLONE_DIR}...")
        run_git("clone", f"https://{BITBUCKET_USERNAME}:{BITBUCKET_APP_PASSWORD}@bitbucket.org/{BITBUCKET_REPO}.git", CLONE_DIR)
    else:
        print(f"Updating existing clone in {CLONE_DIR}...")
        run_git("fetch", "--all", cwd=CLONE_DIR)

def branch_exists(remote_branch):
    try:
        run_git("rev-parse", "--verify", remote_branch, cwd=CLONE_DIR)
        return True
    except RuntimeError:
        return False

def sync_base_branch(base_branch):
    """Push the base branch from Bitbucket to GitHub so GitHub knows its history."""
    try:
        run_git("push", "--force", GITHUB_PUSH_URL, f"origin/{base_branch}:{base_branch}", cwd=CLONE_DIR)
        print(f"🔄 Synced base branch '{base_branch}' to GitHub")
    except Exception as e:
        print(f"⚠️ Could not sync base branch '{base_branch}': {e}")

def create_temp_branch(pr_id, source_branch):
    temp_local_branch = f"temp-src-{pr_id}"
    temp_github_branch = f"bitbucket-pr-{pr_id}"
    remote_branch = f"origin/{source_branch}"

    if not branch_exists(remote_branch):
        raise Exception(f"Remote branch '{remote_branch}' not found. Skipping PR.")

    # Create temporary local branch from remote source
    run_git("checkout", "-B", temp_local_branch, remote_branch, cwd=CLONE_DIR)

    # Amend timestamp to get a unique SHA
    date_str = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    run_git("commit", "--amend", "--no-edit", f"--date={date_str}", cwd=CLONE_DIR)

    # Push it as a temporary GitHub branch
    run_git("push", "--force", GITHUB_PUSH_URL, f"{temp_local_branch}:{temp_github_branch}", cwd=CLONE_DIR)

    return temp_github_branch


# === GITHUB ===

def create_github_pull_request(pr, temp_branch):
    base = pr["destination"]["branch"]["name"]
    title = pr["title"]
    body = f"Imported from Bitbucket PR #{pr['id']}:\n\n{pr.get('description') or ''}"

    payload = {
        "title": title,
        "head": temp_branch,
        "base": base,
        "body": body
    }

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 422:
        raise Exception(f"GitHub PR creation failed: {resp.text}")
    resp.raise_for_status()
    return resp.json()

def close_github_pull_request(pr_number):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    payload = {"state": "closed"}
    resp = requests.patch(url, headers=headers, json=payload)
    resp.raise_for_status()

def delete_github_branch(branch_name):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/{branch_name}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    resp = requests.delete(url, headers=headers)
    if resp.status_code == 204:
        print(f" 🧹 Deleted GitHub branch '{branch_name}'")
    elif resp.status_code == 404:
        print(f" ⚠️ GitHub branch '{branch_name}' not found (already deleted?)")
    else:
        print(f" ⚠️ Could not delete branch '{branch_name}': {resp.text}")

# === MAIN ===

def main():
    ensure_repo_cloned()
    prs = load_or_fetch_bitbucket_prs()
    prs.sort(key=lambda x: x["created_on"])  # oldest to newest

    for pr in prs:
        pr_id = pr['id']
        if pr_id in exclude_prs:
            print(f"⏩ Skipping excluded PR #{pr_id}")
            continue

        if pr_id in transferred_prs:
            print(f"✅ Skipping already transferred PR #{pr_id}")
            continue

        source_branch = pr['source']['branch']['name']
        base_branch = pr['destination']['branch']['name']

        print(f"🚀 Transferring PR #{pr_id}: {pr['title']}")
        print(f" -> Source: {source_branch} | Target: {base_branch}")

        try:
            # Ensure base branch exists in GitHub
            sync_base_branch(base_branch)

            # Create a temporary GitHub-safe branch
            temp_branch = create_temp_branch(pr_id, source_branch)

            # Create PR on GitHub
            created_pr = create_github_pull_request(pr, temp_branch)
            print(f" ✅ Created GitHub PR #{created_pr['number']}")

            # Immediately close it
            close_github_pull_request(created_pr['number'])
            print(f" 🔒 Closed GitHub PR #{created_pr['number']}")

            # Delete temp branch from GitHub
            delete_github_branch(temp_branch)

            save_transferred(pr_id)
            time.sleep(1)

        except Exception as e:
            if "not found. Skipping PR" in str(e):
                print(f"⚠️ Skipping PR #{pr_id}: {e}")
                save_transferred(pr_id)
                continue
            print(f"❌ Error with PR #{pr_id}: {e}")
            break


if __name__ == "__main__":
    main()
