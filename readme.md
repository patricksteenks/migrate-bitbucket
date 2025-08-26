# First start to set up the virtual environment for python

python3 -m venv venv
source venv/bin/activate
pip install dotenv requests

Then run transfer-pull-requests.py

## Set up of other files
Make sure to generate files with empty [] json:
- exclude_prs.json
- transferred_prs.json