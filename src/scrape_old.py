

"""
Manually explore the page to find potential page with the listing
Take the url of the page that contains the listing and parse into the scraper
store all the pages with the listing into a txt/json file

1. pages that contains the listings
2. extract the listing urls from the html
3. put the listing urls into a text/json file
4. crawl all listing urls of a market

Example file: 
    1. {"market": market_name,  "category page": xxx, "listing url": xxx, "html": xxxx.}
    2. {"market": market_name,  "category page": xxx, "listing url": xxx, "title": xxx, "price": xxx}


Also include more fields
    1. Description (Done)
    2. How much left are in stock ( Not Done)
    3. Possibly the reviews (Done)
    4. The category of the product (Done)
    5. Price table (Done)
    6. Extract title from listing page (Done)
    7. Apply the filter to the rest of marketplaces. 

Use Beautiful Soup to parse HTML
"""


import random
import time
import pickle
import requests
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import TimeoutException
import argparse
from bs4 import BeautifulSoup
from termcolor import colored
import urllib.parse
import re

# Proxy setup defaults
proxy_host = "127.0.0.1"
proxy_port = 8118
tor_socks_port = 9050

# JSON file to store scraped products
products_output_file = "products.json"
# JSON file to store full pages (HTML + metadata)
pages_output_file = "scraped_pages.json"

# JSON file to store keyword-specific URLs
keyword_urls_file = "pages_url.json"
# JSON file to store raw HTML for product listings
products_html_output_file = "products_html.json"

# Global flag set in main()
save_pages = False

# Helper functions to manage JSON storage
import json
import tempfile
import os

def load_saved_products():
    """Load saved products from the JSON file."""
    if not os.path.exists(products_output_file):
        return []
    try:
        with open(products_output_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save_products_atomic(products):
    """Atomically write the list of products to the JSON file."""
    dirpath = os.path.dirname(os.path.abspath(products_output_file)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix='tmp_products_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            json.dump(products, tmp_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, products_output_file)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def save_keyword_urls_atomic(urls):
    """Atomically write the list of keyword-found URLs to the JSON file."""
    dirpath = os.path.dirname(os.path.abspath(keyword_urls_file)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix='tmp_keywords_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            json.dump(urls, tmp_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, keyword_urls_file)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def load_saved_product_html():
    if not os.path.exists(products_html_output_file):
        return []
    try:
        with open(products_html_output_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_product_html_atomic(entries):
    dirpath = os.path.dirname(os.path.abspath(products_html_output_file)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix='tmp_product_html_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            json.dump(entries, tmp_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, products_html_output_file)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def load_saved_pages():
    if not os.path.exists(pages_output_file):
        return []
    try:
        with open(pages_output_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_pages_atomic(pages):
    dirpath = os.path.dirname(os.path.abspath(pages_output_file)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix='tmp_pages_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            json.dump(pages, tmp_file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, pages_output_file)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# File to track last URL scraped
checkpoint_file = "scraping_checkpoint.pkl"

# Note: webdriver is created inside main() so we can configure proxy mode and timeouts at runtime.

# Target URL
start_url = "http://drugj7dwjgdxyrqlciswny7ioa6wt2bbljifqspw2mg2cxv4n36ihcyd.onion/shop/high-quality-ultima-clomid-in-the-usa/"

# Load checkpoint if it exists
def load_checkpoint():
    try:
        with open(checkpoint_file, 'rb') as f:
            return pickle.load(f)
    except FileNotFoundError:
        return [start_url]

# Save the current scraping state to resume later
def save_checkpoint(url_queue):
    with open(checkpoint_file, 'wb') as f:
        pickle.dump(url_queue, f)

# Function to clean text by removing unwanted characters
def clean_text(text):
    text = re.sub(r'[\n\r]+', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

def canonicalize_path(url_or_path):
    """Normalize a URL path so comparisons ignore trailing slashes."""
    if not url_or_path:
        return '/'
    parsed = urllib.parse.urlparse(url_or_path)
    path = parsed.path if parsed.scheme or parsed.netloc else url_or_path
    if not path:
        return '/'
    if not path.startswith('/'):
        path = '/' + path
    path = path.rstrip('/')
    return path or '/'

# Function to extract cookies and close the browser
def extract_cookies(driver):
    cookies = driver.get_cookies()
    driver.quit()
    return {cookie['name']: cookie['value'] for cookie in cookies}


def extract_cookies(driver, do_quit=True):
    """Return cookies from the Selenium driver as a dict. If do_quit is True, quit the driver."""
    cookies = driver.get_cookies()
    cookie_dict = {cookie['name']: cookie['value'] for cookie in cookies}
    if do_quit:
        try:
            driver.quit()
        except Exception:
            pass
    return cookie_dict

# Set up session with cookies
def setup_requests_session(cookies):
    session = requests.Session()
    session.proxies = {'http': f'http://{proxy_host}:{proxy_port}', 'https': f'http://{proxy_host}:{proxy_port}'}
    session.cookies.update(cookies)
    return session

# Scrape post content
def scrape_post_content(session, post_url, retries=3):
    attempt = 0
    while attempt < retries:
        try:
            response = session.get(post_url, timeout=30)
            if response.status_code == 200:
                # Optionally save the raw HTML of the fetched page
                if save_pages:
                    try:
                        pages = load_saved_pages()
                        pages.append({
                            'url': post_url,
                            'timestamp': int(time.time()),
                            'html': response.text
                        })
                        save_pages_atomic(pages)
                    except Exception as e:
                        print(colored(f"Failed saving fetched page to pages JSON: {e}", "red"))

                soup = BeautifulSoup(response.content, 'html.parser')
                content = soup.find('div', class_='postContent').get_text(separator="\n")
                return clean_text(content)
            else:
                print(colored(f"Warning: Failed to retrieve content from {post_url}", "yellow"))
        except requests.exceptions.RequestException as e:
            attempt += 1
            print(colored(f"Connection error on {post_url}, retry {attempt}/{retries}: {e}", "red"))
            time.sleep(5)  # Wait before retrying
    return ""


def extract_product_details(soup, url):
    """
    Flexible product detail extraction using multiple strategies.
    Returns a dictionary with all available product information.
    """
    details = {}
    
    # --- TITLE EXTRACTION ---
    title_selectors = [
        'h1.product_title',
        'h1.entry-title',
        'h2.woocommerce-loop-product__title',
        '[class*="product"][class*="title"]',
        'h1[itemprop="name"]',
        '.product-title',
        'h1'
    ]
    
    for selector in title_selectors:
        title_elem = soup.select_one(selector)
        if title_elem:
            details['title'] = clean_text(title_elem.get_text())
            break
    
    # --- PRICE EXTRACTION ---
    price_selectors = [
        '.summary .price',
        '.product .price',
        'p.price',
        'span.price',
        '[class*="price"]',
        '[itemprop="price"]'
    ]
    
    for selector in price_selectors:
        price_elem = soup.select_one(selector)
        if price_elem:
            price_text = clean_text(price_elem.get_text())
            # Extract numeric price using regex
            price_match = re.search(r'[\$€£¥]?\s*(\d+[.,]?\d*)', price_text)
            if price_match:
                details['price'] = price_text
                details['price_numeric'] = price_match.group(1).replace(',', '')
            break
    
    # --- STOCK/AVAILABILITY EXTRACTION ---
    stock_patterns = [
        (r'(\d+)\s*in\s*stock', 'in_stock_count'),
        (r'(\d+)\s*available', 'in_stock_count'),
        (r'(\d+)\s*left', 'in_stock_count'),
        (r'stock:\s*(\d+)', 'in_stock_count'),
        (r'(in\s*stock)', 'stock_status'),
        (r'(out\s*of\s*stock)', 'stock_status'),
        (r'(available)', 'stock_status'),
        (r'(unavailable)', 'stock_status'),
    ]
    
    # Check stock elements
    stock_selectors = [
        '.stock',
        'p.stock',
        '.availability',
        '[class*="stock"]',
        '.in-stock',
        '.out-of-stock'
    ]
    
    for selector in stock_selectors:
        stock_elem = soup.select_one(selector)
        if stock_elem:
            stock_text = clean_text(stock_elem.get_text())
            details['stock_raw'] = stock_text
            
            # Try to extract specific stock information
            for pattern, key in stock_patterns:
                match = re.search(pattern, stock_text, re.IGNORECASE)
                if match:
                    details[key] = match.group(1)
                    break
            break
    
    # --- DESCRIPTION EXTRACTION ---
    description_selectors = [
        '.woocommerce-Tabs-panel--description',
        '.product-description',
        '[id*="description"]',
        '.entry-content',
        '.product-detail',
        '.description',
        '[itemprop="description"]'
    ]
    
    for selector in description_selectors:
        desc_elem = soup.select_one(selector)
        if desc_elem:
            # Get text but preserve some structure
            desc_text = desc_elem.get_text(separator='\n', strip=True)
            details['description'] = clean_text(desc_text)
            
            # Also extract key-value pairs (manufacturer, substance, package, etc.)
            desc_lines = [line.strip() for line in desc_text.split('\n') if line.strip()]
            for line in desc_lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower().replace(' ', '_')
                    value = value.strip()
                    details[f'desc_{key}'] = value
            break
    
    # --- CATEGORY EXTRACTION ---
    category_selectors = [
        '.posted_in a',
        '.product_cat a',
        '[class*="category"] a',
        '.breadcrumb a',
        '[rel="tag"]'
    ]
    
    categories = []
    for selector in category_selectors:
        cat_elems = soup.select(selector)
        for elem in cat_elems:
            cat_text = clean_text(elem.get_text())
            if cat_text and cat_text.lower() not in ['home', 'shop']:
                categories.append(cat_text)
    
    if categories:
        details['categories'] = list(dict.fromkeys(categories))  # Remove duplicates
        details['primary_category'] = categories[-1] if categories else None
    
    # --- SKU EXTRACTION ---
    sku_selectors = [
        '.sku',
        '[class*="sku"]',
        '[itemprop="sku"]'
    ]
    
    for selector in sku_selectors:
        sku_elem = soup.select_one(selector)
        if sku_elem:
            details['sku'] = clean_text(sku_elem.get_text())
            break
    
    # --- REVIEWS/RATING EXTRACTION ---
    # Star rating
    rating_elem = soup.select_one('.star-rating, [class*="rating"]')
    if rating_elem:
        # Try to extract rating from style width
        style = rating_elem.get('style', '')
        width_match = re.search(r'width:\s*(\d+)%', style)
        if width_match:
            details['rating_percent'] = width_match.group(1)
            details['rating_stars'] = float(width_match.group(1)) / 20  # Convert to 5-star scale
        
        # Also check for text rating
        rating_text = rating_elem.get_text()
        rating_match = re.search(r'(\d+\.?\d*)\s*out\s*of\s*5', rating_text)
        if rating_match:
            details['rating_stars'] = float(rating_match.group(1))
    
    # Review count
    review_count_elem = soup.select_one('.woocommerce-review-link, [class*="review"][class*="count"]')
    if review_count_elem:
        review_text = review_count_elem.get_text()
        count_match = re.search(r'(\d+)', review_text)
        if count_match:
            details['review_count'] = int(count_match.group(1))
    
    # Extract individual reviews
    review_elems = soup.select('.comment, .review, [class*="review-"]')
    if review_elems:
        reviews = []
        for review_elem in review_elems[:10]:  # Limit to first 10 reviews
            review_data = {}
            
            # Review text
            review_text_elem = review_elem.select_one('.comment-text, .review-text, [class*="description"]')
            if review_text_elem:
                review_data['text'] = clean_text(review_text_elem.get_text())
            
            # Reviewer name
            author_elem = review_elem.select_one('.comment-author, .reviewer, [class*="author"]')
            if author_elem:
                review_data['author'] = clean_text(author_elem.get_text())
            
            # Review rating
            review_rating_elem = review_elem.select_one('.star-rating')
            if review_rating_elem:
                style = review_rating_elem.get('style', '')
                width_match = re.search(r'width:\s*(\d+)%', style)
                if width_match:
                    review_data['rating'] = float(width_match.group(1)) / 20
            
            if review_data:
                reviews.append(review_data)
        
        if reviews:
            details['reviews'] = reviews
    
    # --- PRICE TABLE/VARIATIONS EXTRACTION ---
    # Check for variation tables
    variation_table = soup.select_one('.variations, [class*="variation"][class*="table"], table.woocommerce-variation')
    if variation_table:
        variations = []
        rows = variation_table.select('tr')
        for row in rows:
            cells = row.select('td, th')
            if len(cells) >= 2:
                var_data = {
                    'option': clean_text(cells[0].get_text()),
                    'value': clean_text(cells[1].get_text())
                }
                variations.append(var_data)
        
        if variations:
            details['variations'] = variations
    
    # Check for quantity pricing
    price_table = soup.select('.woocommerce-table, table[class*="price"]')
    if price_table:
        price_tiers = []
        for table in price_table:
            rows = table.select('tr')
            for row in rows:
                cells = row.select('td')
                if len(cells) >= 2:
                    # Try to extract quantity and price
                    qty_text = clean_text(cells[0].get_text())
                    price_text = clean_text(cells[1].get_text())
                    
                    qty_match = re.search(r'(\d+)', qty_text)
                    price_match = re.search(r'[\$€£¥]?\s*(\d+[.,]?\d*)', price_text)
                    
                    if qty_match and price_match:
                        price_tiers.append({
                            'quantity': qty_match.group(1),
                            'price': price_match.group(1)
                        })
        
        if price_tiers:
            details['price_tiers'] = price_tiers
    
    # --- IMAGES ---
    image_selectors = [
        '.woocommerce-product-gallery__image img',
        '.product-image img',
        '[class*="product"][class*="gallery"] img',
        '.wp-post-image'
    ]
    
    images = []
    for selector in image_selectors:
        img_elems = soup.select(selector)
        for img in img_elems:
            img_url = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if img_url and 'placeholder' not in img_url:
                images.append(img_url)
    
    if images:
        details['images'] = list(dict.fromkeys(images))  # Remove duplicates
        details['main_image'] = images[0] if images else None
    
    return details


def parse_and_save_products(html, base_url, scraped_products, session=None):
    """Parse provided HTML, persist product metadata, and archive listing HTML."""
    soup = BeautifulSoup(html, 'html.parser')

    products_cache = getattr(parse_and_save_products, 'products_cache', load_saved_products())
    saved_product_urls = getattr(parse_and_save_products, 'saved_urls', {p.get('listing url') for p in products_cache if p.get('listing url')})

    product_html_cache = getattr(parse_and_save_products, 'products_html_cache', load_saved_product_html())
    saved_html_urls = getattr(parse_and_save_products, 'saved_html_urls', {p.get('listing url') for p in product_html_cache if p.get('listing url')})

    def save_product_record(record):
        listing_url = record.get('listing url')
        if listing_url in saved_product_urls:
            return False
        products_cache.append(record)
        save_products_atomic(products_cache)
        saved_product_urls.add(listing_url)
        parse_and_save_products.products_cache = products_cache
        parse_and_save_products.saved_urls = saved_product_urls
        return True

    def ensure_product_html(listing_url, market_name, category_page, fallback_html=None):
        if listing_url in saved_html_urls:
            return False

        html_text = fallback_html
        fetched_remotely = False

        if not html_text and session:
            try:
                detail_resp = session.get(listing_url, timeout=25)
                if detail_resp.status_code == 200:
                    html_text = detail_resp.text
                    fetched_remotely = True
                else:
                    print(colored(f"Failed to fetch listing HTML ({detail_resp.status_code}): {listing_url}", "yellow"))
            except requests.exceptions.RequestException as exc:
                print(colored(f"Error fetching listing HTML {listing_url}: {exc}", "red"))

        if not html_text:
            return False

        # Extract detailed product information from the HTML
        detail_soup = BeautifulSoup(html_text, 'html.parser')
        product_details = extract_product_details(detail_soup, listing_url)

        html_record = {
            "market": market_name,
            "category page": category_page,
            "listing url": listing_url,
            "fetched_at": int(time.time()),
            "html": html_text,
            **product_details  # Include all extracted details
        }

        product_html_cache.append(html_record)
        save_product_html_atomic(product_html_cache)
        saved_html_urls.add(listing_url)
        parse_and_save_products.products_html_cache = product_html_cache
        parse_and_save_products.saved_html_urls = saved_html_urls
        print(colored(f"Stored HTML for: {listing_url}", "blue"))
        
        # Print extracted details for debugging
        if product_details:
            print(colored(f"  Extracted: {', '.join(product_details.keys())}", "cyan"))

        if fetched_remotely:
            time.sleep(random.uniform(1, 2.5))

        return True

    market_name = base_url.split('/')[2]

    # --- FLEXIBLE PRODUCT LISTING DETECTION ---
    # Try multiple selectors for product listings
    product_selectors = [
        'li.product',
        '.product-item',
        '[class*="product"][class*="item"]',
        '.woocommerce-product',
        'article.product',
        '.listing-item'
    ]
    
    products = []
    for selector in product_selectors:
        products = soup.select(selector)
        if products:
            print(colored(f"Found {len(products)} products using selector: {selector}", "cyan"))
            break

    for product in products:
        try:
            # Extract title with flexible selectors
            title_selectors = [
                'h2.woocommerce-loop-product__title',
                '.product-title',
                'h2',
                'h3',
                '[class*="title"]'
            ]
            title_element = None
            for sel in title_selectors:
                title_element = product.select_one(sel)
                if title_element:
                    break
            
            # Extract price with flexible selectors
            price_selectors = [
                '.price',
                '[class*="price"]',
                '.product-price',
                '.cost'
            ]
            price_element = None
            for sel in price_selectors:
                price_element = product.select_one(sel)
                if price_element:
                    break
            
            # Extract link with flexible selectors
            link_element = product.select_one('a[href]')

            if not (title_element and link_element):
                continue

            title = title_element.get_text(strip=True)
            price = price_element.get_text(strip=True) if price_element else 'N/A'
            listing_url = urllib.parse.urljoin(base_url, link_element['href'])

            product_document = {
                "market": market_name,
                "category page": base_url,
                "listing url": listing_url,
                "title": title,
                "price": price
            }

            if save_product_record(product_document):
                print(colored(f"Product saved: {title}", "green"))
            else:
                print(colored(f"Skipping duplicate product: {title}", "yellow"))

            ensure_product_html(listing_url, market_name, base_url)

        except Exception as e:
            print(colored(f"Error parsing product: {e}", "red"))

    # Handle single product detail pages if no listing items were found
    if not products:
        try:
            # Use the new flexible extraction function
            product_details = extract_product_details(soup, base_url)
            
            if product_details.get('title'):
                product_document = {
                    "market": market_name,
                    "category page": base_url,
                    "listing url": base_url,
                    **product_details
                }

                if save_product_record(product_document):
                    print(colored(f"Product saved: {product_details.get('title')}", "green"))
                else:
                    print(colored(f"Product already stored for URL: {base_url}", "yellow"))

                ensure_product_html(base_url, market_name, base_url, fallback_html=html)
        except Exception as e:
            print(colored(f"Error parsing standalone product detail: {e}", "red"))

    # --- Pagination Logic (remains the same) ---
    pagination_selectors = [
        'div.pagination a', 'ul.pagination a', 'a[rel="next"]', 'a.next',
        'li.next a', 'nav a', 'a[aria-label="Next"]'
    ]
    next_pages = []
    for sel in pagination_selectors:
        links = soup.select(sel)
        for link in links:
            href = link.get('href')
            if href:
                next_pages.append(urllib.parse.urljoin(base_url, href))
        if next_pages:
            break

    if not next_pages:
        for a in soup.find_all('a', href=True):
            href = a['href']
            if re.search(r'(/page/|\?page=|page=\d+)', href):
                next_pages.append(urllib.parse.urljoin(base_url, href))

    next_pages = list(dict.fromkeys(next_pages))
    print(colored(f"Found pagination links: {next_pages}", "blue"))
    return next_pages

def scrape_product_page(html):
    """
    Scrapes the content of a single product page.
    Looks for a div with class 'product-detail'.
    """
    soup = BeautifulSoup(html, 'html.parser')
    content_div = soup.find('div', class_='product-detail') # Assumption based on common product page structure
    if not content_div:
        # Fallback to other common content containers if the primary one isn't found
        content_div = soup.find('div', id='content') or soup.find('div', class_='content') or soup.find('main')

    if content_div:
        return clean_text(content_div.get_text(separator="\n"))
    else:
        print(colored("Could not find the main content container on the product page.", "red"))
        return ""

# Scrape a page and retrieve product data
def scrape_page(session, url, scraped_pages, allowed_paths=None, retries=3, selenium_driver=None):
    attempt = 0
    while attempt < retries:
        try:
            response = session.get(url, timeout=20)
            if response.status_code == 200:
                if save_pages:
                    try:
                        pages = load_saved_pages()
                        pages.append({'url': url, 'timestamp': int(time.time()), 'html': response.text})
                        save_pages_atomic(pages)
                    except Exception as e:
                        print(colored(f"Failed saving fetched page to pages JSON: {e}", "red"))

                next_pages = parse_and_save_products(response.text, url, scraped_pages, session=session)
                if allowed_paths:
                    next_pages = [link for link in next_pages if canonicalize_path(link) in allowed_paths]
                scraped_pages[url] = True
                return next_pages
            else:
                print(colored(f"Failed to scrape {url}, status code: {response.status_code}", "red"))
        except requests.exceptions.RequestException as e:
            attempt += 1
            print(colored(f"Connection error on {url}, retry {attempt}/{retries}: {e}", "red"))
            time.sleep(5)

    if selenium_driver:
        try:
            print(colored(f"Requests failed for {url}; attempting Selenium fallback...", "yellow"))
            selenium_driver.get(url)
            time.sleep(2)
            html = selenium_driver.page_source
            if save_pages:
                try:
                    pages = load_saved_pages()
                    pages.append({'url': url, 'timestamp': int(time.time()), 'html': html})
                    save_pages_atomic(pages)
                except Exception as e:
                    print(colored(f"Failed saving selenium-fetched page to pages JSON: {e}", "red"))

            next_pages = parse_and_save_products(html, url, scraped_pages, session=session)
            if allowed_paths:
                next_pages = [link for link in next_pages if canonicalize_path(link) in allowed_paths]
            scraped_pages[url] = True
            return next_pages
        except Exception as se:
            print(colored(f"Selenium fallback also failed for {url}: {se}", "red"))

    return []

# Main function
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manual', action='store_true', help='Open browser and wait for manual CAPTCHA solving before continuing')
    parser.add_argument('--socks', action='store_true', help='Use Tor SOCKS5 (default uses HTTP proxy on 8118)')
    parser.add_argument('--socks-port', type=int, default=tor_socks_port, help='Tor SOCKS port (default 9050)')
    parser.add_argument('--page-timeout', type=int, default=300, help='Selenium page load timeout in seconds')
    parser.add_argument('--tor-binary', type=str, default=None, help='Path to Tor Browser firefox binary (e.g. /Applications/Tor Browser.app/Contents/MacOS/firefox)')
    parser.add_argument('--tor-profile', type=str, default=None, help='Path to Tor Browser profile directory to reuse')
    parser.add_argument('--disable-js', action='store_true', help='Disable JavaScript in the browser (use page HTML only)')
    parser.add_argument('--save-pages', action='store_true', help='Save full page HTML and metadata to a JSON file')
    parser.add_argument('--selenium-fallback', action='store_true', help='When requests fail, fetch pages with Selenium as a fallback')
    parser.add_argument('--search-keywords', nargs='+', help='Crawl the site to find pages containing these keywords and save their URLs to pages_url.json.')
    parser.add_argument('--category-endpoints', nargs='+', help='Restrict scraping to these exact endpoints (e.g. /sex-aids /buy-steroids).')
    args = parser.parse_args()

    # Track scraped pages to avoid reprocessing
    scraped_pages = {}
    global save_pages
    save_pages = args.save_pages

    using_endpoints = bool(args.category_endpoints)
    base_parts = urllib.parse.urlparse(start_url)
    base_url = f"{base_parts.scheme}://{base_parts.netloc}"
    allowed_paths = set()
    target_urls = []
    if using_endpoints:
        for endpoint in args.category_endpoints:
            canonical = canonicalize_path(endpoint)
            if canonical in allowed_paths:
                continue
            allowed_paths.add(canonical)
            full_url = urllib.parse.urljoin(base_url.rstrip('/') + '/', canonical.lstrip('/'))
            if endpoint and endpoint.endswith('/') and not full_url.endswith('/'):
                full_url = full_url.rstrip('/') + '/'
            target_urls.append(full_url)
        if not target_urls:
            print(colored("No valid category endpoints resolved; defaulting to checkpoint-driven crawl.", "yellow"))
            using_endpoints = False
            allowed_paths.clear()

    initial_browser_url = target_urls[0] if target_urls else start_url
    if using_endpoints:
        print(colored(f"Restricting crawl to endpoints: {', '.join(sorted(allowed_paths))}", "cyan"))
        print(colored(f"Target category URLs: {', '.join(target_urls)}", "cyan"))
        if target_urls:
            save_keyword_urls_atomic(list(dict.fromkeys(target_urls)))

    try:
        # Initialize Firefox Options and proxy settings
        options = Options()
        # Allow using Tor Browser binary if provided
        if args.tor_binary:
            options.binary_location = args.tor_binary
            print(colored(f"Using Tor Browser binary: {args.tor_binary}", "yellow"))
            print(colored("Make sure geckodriver is compatible with Tor Browser's Firefox binary.", "yellow"))
        else:
            options.binary_location = "/Applications/Firefox.app/Contents/MacOS/firefox"
        options.set_preference("network.proxy.type", 1)
        if args.socks:
            # Use Tor SOCKS proxy for both HTTP and HTTPS
            options.set_preference("network.proxy.socks", proxy_host)
            options.set_preference("network.proxy.socks_port", args.socks_port)
            options.set_preference("network.proxy.socks_version", 5)
            # do not use http/https proxies
            options.set_preference("network.proxy.http", "")
            options.set_preference("network.proxy.http_port", 0)
            options.set_preference("network.proxy.ssl", "")
            options.set_preference("network.proxy.ssl_port", 0)
        else:
            # Use HTTP proxy (e.g., Privoxy) on proxy_host:proxy_port
            options.set_preference("network.proxy.http", proxy_host)
            options.set_preference("network.proxy.http_port", proxy_port)
            options.set_preference("network.proxy.ssl", proxy_host)
            options.set_preference("network.proxy.ssl_port", proxy_port)
            # Point socks prefs to proxy_host as a fallback
            options.set_preference("network.proxy.socks", proxy_host)
            options.set_preference("network.proxy.socks_port", proxy_port)
            options.set_preference("network.proxy.socks_version", 5)
        options.set_preference("network.proxy.no_proxies_on", "")

        # Optionally disable JavaScript (may be overridden by extensions like NoScript in Tor Browser)
        if args.disable_js:
            try:
                options.set_preference('javascript.enabled', False)
                print(colored('JavaScript will be disabled in the browser (javascript.enabled = false).', 'yellow'))
            except Exception:
                print(colored('Failed to set javascript.enabled preference; Tor Browser/NoScript may override this.', 'yellow'))

        # If a Tor profile path is provided, use it to preserve Tor Browser settings
        firefox_profile = None
        if args.tor_profile:
            try:
                from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
                firefox_profile = FirefoxProfile(args.tor_profile)
                print(colored(f"Using Tor Browser profile: {args.tor_profile}", "yellow"))
            except Exception as e:
                print(colored(f"Failed to load Tor profile: {e}", "red"))

        if firefox_profile:
            driver = webdriver.Firefox(firefox_profile=firefox_profile, options=options)
        else:
            driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(args.page_timeout)

        print(colored(f"Opening URL: {initial_browser_url}", "blue"))
        try:
            driver.get(initial_browser_url)
        except TimeoutException:
            # Allow long manual solving: stop loading and continue
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            print(colored("Navigation timed out; stopped page load. You may proceed to solve CAPTCHAs manually.", "yellow"))

        initial_page_html = None

        print(colored("Waiting 60 seconds to ensure the session is established...", "yellow"))
        time.sleep(60)
        # Manual mode: let user solve CAPTCHA in the opened browser, then capture current page
        if args.manual:
            print(colored("Manual mode: please solve any CAPTCHA in the opened browser. Press Enter here to continue.", "yellow"))
            input()

            try:
                page_html = driver.page_source
            except Exception as e:
                print(colored(f"Failed to access driver.page_source: {e}", "red"))
                page_html = None

            if page_html:
                initial_page_html = page_html
                dump_path = f"manual_page_{int(time.time())}.html"
                try:
                    with open(dump_path, 'w', encoding='utf-8') as fh:
                        fh.write(page_html)
                    print(colored(f"Saved current page HTML to: {dump_path}", "green"))
                except Exception as e:
                    print(colored(f"Failed to write page dump to file: {e}", "red"))

                # Optionally save full page HTML+metadata to pages JSON
                if save_pages:
                    try:
                        pages = load_saved_pages()
                        pages.append({
                            'url': initial_browser_url,
                            'timestamp': int(time.time()),
                            'html': page_html
                        })
                        save_pages_atomic(pages)
                        print(colored(f"Saved full page HTML to {pages_output_file}", "green"))
                    except Exception as e:
                        print(colored(f"Failed saving page to pages JSON: {e}", "red"))

        # Extract cookies but don't quit the browser yet (we will close in finally)
        cookies = extract_cookies(driver, do_quit=False)

        # Setup requests to use Tor SOCKS if requested
        if args.socks:
            try:
                import socks
            except Exception:
                print(colored("pysocks not installed. Install with: pip install pysocks requests[socks]", "red"))
                return
            session = requests.Session()
            session.proxies = {
                'http': f'socks5h://{proxy_host}:{args.socks_port}',
                'https': f'socks5h://{proxy_host}:{args.socks_port}'
            }
            session.cookies.update(cookies)
        else:
            session = setup_requests_session(cookies)

        # Optionally create a Selenium fallback driver to render pages that requests cannot fetch
        fallback_driver = None
        if args.selenium_fallback:
            try:
                # reuse options used earlier for main driver settings
                fb_options = Options()
                if args.tor_binary:
                    fb_options.binary_location = args.tor_binary
                fb_options.set_preference("network.proxy.type", 1)
                if args.socks:
                    fb_options.set_preference("network.proxy.socks", proxy_host)
                    fb_options.set_preference("network.proxy.socks_port", args.socks_port)
                    fb_options.set_preference("network.proxy.socks_version", 5)
                else:
                    fb_options.set_preference("network.proxy.http", proxy_host)
                    fb_options.set_preference("network.proxy.http_port", proxy_port)
                fb_options.set_preference("network.proxy.no_proxies_on", "")
                if args.disable_js:
                    fb_options.set_preference('javascript.enabled', False)
                fallback_driver = webdriver.Firefox(options=fb_options)
                fallback_driver.set_page_load_timeout(min(60, args.page_timeout))
                print(colored("Selenium fallback driver started.", "yellow"))
            except Exception as e:
                print(colored(f"Failed to start Selenium fallback driver: {e}", "red"))

        if using_endpoints:
            to_scrape = list(dict.fromkeys(target_urls))
        else:
            to_scrape = load_checkpoint()
            if initial_page_html:
                try:
                    initial_next_pages = parse_and_save_products(initial_page_html, initial_browser_url, scraped_pages, session=session)
                    if initial_next_pages:
                        for link in initial_next_pages:
                            if link not in to_scrape:
                                to_scrape.append(link)
                        save_checkpoint(to_scrape)
                        print(colored(f"Enqueued {len(initial_next_pages)} pages discovered during manual setup.", "green"))
                except Exception as exc:
                    print(colored(f"Failed to parse initial manual page: {exc}", "red"))
        scraped = set()
        # Load already saved products and initialize saved_urls set to prevent duplicates
        existing_products = load_saved_products()
        saved_urls = {p.get('listing url') for p in existing_products if p.get('listing url')}
        scrape_page.saved_urls = saved_urls

        while to_scrape:
            url = to_scrape.pop(0)
            if url not in scraped:
                print(colored(f"Scraping page: {url}", "magenta"))
                allowed = allowed_paths if using_endpoints else None
                new_links = scrape_page(session, url, scraped_pages, allowed_paths=allowed, selenium_driver=fallback_driver)

                # Avoid duplicates in to_scrape
                to_scrape.extend(link for link in new_links if link not in scraped and link not in to_scrape)
                scraped.add(url)

                if using_endpoints and new_links:
                    added = 0
                    for link in new_links:
                        if link not in target_urls:
                            target_urls.append(link)
                            added += 1
                    if added:
                        save_keyword_urls_atomic(list(dict.fromkeys(target_urls)))

                if not using_endpoints:
                    save_checkpoint(to_scrape)
                time.sleep(random.uniform(5, 15))

    except Exception as e:
        print(colored(f"Error in main scraping function: {e}", "red"))
    finally:
        # ensure both drivers are quit if they were started
        try:
            if 'driver' in locals() and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if 'fallback_driver' in locals() and fallback_driver:
                try:
                    fallback_driver.quit()
                except Exception:
                    pass
        except Exception:
            pass

def keyword_search_mode(args, options):
    """Crawl the site to find pages matching keywords."""
    print(colored(f"Starting keyword search for: {args.search_keywords}", "cyan"))
    
    driver = None
    try:
        driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(args.page_timeout)
        print(colored(f"Opening start URL to establish session: {start_url}", "blue"))
        try:
            driver.get(start_url)
        except TimeoutException:
            driver.execute_script("window.stop();")
            print(colored("Page load timed out, stopping load to proceed.", "yellow"))

        if args.manual:
            print(colored("Manual mode: please solve any CAPTCHA. Press Enter here to continue.", "yellow"))
            input()

        cookies = extract_cookies(driver, do_quit=True)
        driver = None # Driver's job is done

        session = requests.Session()
        if args.socks:
            session.proxies = {'http': f'socks5h://{proxy_host}:{args.socks_port}', 'https': f'socks5h://{proxy_host}:{args.socks_port}'}
        else:
            session.proxies = {'http': f'http://{proxy_host}:{proxy_port}', 'https': f'http://{proxy_host}:{proxy_port}'}
        session.cookies.update(cookies)
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0'})

        to_visit = [start_url]
        visited = set()
        found_urls = []
        keywords_lower = [k.lower() for k in args.search_keywords]

        while to_visit:
            url = to_visit.pop(0)
            if url in visited:
                continue
            
            visited.add(url)
            print(colored(f"Searching: {url}", "magenta"))

            try:
                response = session.get(url, timeout=20)
                if response.status_code != 200:
                    continue

                html = response.text
                html_lower = html.lower()
                
                # Check for keywords
                if any(keyword in html_lower for keyword in keywords_lower):
                    print(colored(f"Found keyword on: {url}", "green"))
                    if url not in found_urls:
                        found_urls.append(url)
                        save_keyword_urls_atomic(found_urls)

                # Find and enqueue new links
                soup = BeautifulSoup(html, 'html.parser')
                for link in soup.find_all('a', href=True):
                    new_url = urllib.parse.urljoin(url, link['href'])
                    # Basic filter to stay on the same site and avoid noise
                    if new_url.startswith(start_url.split('/')[0] + '//' + start_url.split('/')[2]) and new_url not in visited:
                        to_visit.append(new_url)
                
                time.sleep(random.uniform(1, 3))

            except requests.RequestException as e:
                print(colored(f"Error visiting {url}: {e}", "red"))
        
        print(colored(f"Keyword search finished. Found {len(found_urls)} matching URLs.", "blue"))

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--dump':
        products = load_saved_products()
        print(json.dumps(products, ensure_ascii=False, indent=2))
    else:
        # A bit of a hack to check for keyword search mode before main() parsing
        if '--search-keywords' in sys.argv:
            parser = argparse.ArgumentParser()
            parser.add_argument('--socks', action='store_true')
            parser.add_argument('--socks-port', type=int, default=tor_socks_port)
            parser.add_argument('--page-timeout', type=int, default=300)
            parser.add_argument('--manual', action='store_true')
            parser.add_argument('--search-keywords', nargs='+')
            parser.add_argument('--category-endpoints', nargs='+')
            args, _ = parser.parse_known_args()

            options = Options()
            options.set_preference("network.proxy.type", 1)
            if args.socks:
                options.set_preference("network.proxy.socks", proxy_host)
                options.set_preference("network.proxy.socks_port", args.socks_port)
                options.set_preference("network.proxy.socks_version", 5)
            else:
                options.set_preference("network.proxy.http", proxy_host)
                options.set_preference("network.proxy.http_port", proxy_port)

            keyword_search_mode(args, options)
        else:
            main()
