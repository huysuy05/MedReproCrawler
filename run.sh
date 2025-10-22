pgrep -a tor || ps aux | grep -i tor | grep -v grep

# Check listening ports (look for 9050 or 9150)
lsof -nP -iTCP -sTCP:LISTEN | grep -E '9050|9150' || netstat -an | grep LISTEN | grep -E '9050|9150'

# Run scrape.py
python scrape_simple.py --socks --socks-port 9050 --manual --disable-js