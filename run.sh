pgrep -a tor || ps aux | grep -i tor | grep -v grep

# Check listening ports (look for 9050 or 9150)
lsof -nP -iTCP -sTCP:LISTEN | grep -E '9050|9150' || netstat -an | grep LISTEN | grep -E '9050|9150'

# Start tor and privoxy in HomeBrew
brew services start tor
brew services start privoxy

# Run scrape.py
# Port choice: 9050 is system Tor (Homebrew). 9150 is Tor Browser's own Tor.
# These are independent Tor clients with separate circuits and hidden-service
# descriptor caches, so a site that times out on one may work on the other.
# If a .onion times out on 9050 but loads in Tor Browser, switch to 9150 and
# keep Tor Browser open while scraping (port 9150 only exists while it runs).
#
# --insecure: most markets use self-signed TLS certs (the .onion address already
# provides authentication), which would otherwise fail requests' cert verification.
python src/scrape_simple.py --socks --socks-port 9150 --manual --disable-js --insecure