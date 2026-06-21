#!/usr/bin/env python3
"""
HTML Parser for Drug Marketplace Data

This parser extracts drug information from HTML scraped from various dark web marketplaces.
It handles different HTML structures and extracts:
- Market name
- Listing title
- Price/price range
- Dosage information
- Rating (if available)
- Reviews (if available)
- Drug description
- Number in stock

TODO
- Add additional attributes:
  - Category (already added via category_page)
  - Any other fields from different marketplaces (Countries ship from, etc)
  - Vendor
  - (Maybe) Format the time crawled/parsed into a better format for easier analysis.
  - More focused in contraceptives
- Consider removing data processing (eg. Splitting, stripping, etc) to store raw data
"""

import argparse
import json
import re
import os
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from termcolor import colored


def clean_text(text):
    """Clean and normalize text content"""
    if not text:
        return ""
    
    # Remove extra whitespace and normalize
    text = re.sub(r'\s+', ' ', text.strip())
    # Remove HTML entities
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#8211;', '-').replace('&#8217;', "'").replace('&#8220;', '"').replace('&#8221;', '"')
    
    return text.strip()


def extract_market_name(soup):
    """Extract the actual marketplace name from HTML title or other elements"""
    if not soup:
        return ""
    
    # Try to extract from title tag first
    title = soup.find('title')
    if title:
        title_text = clean_text(title.get_text())
        
        # Pattern for X Wave Market: "Product Name - THE X WAVE MARKET"
        if ' - THE X WAVE MARKET' in title_text:
            return "THE X WAVE MARKET"

        # Pattern for Osiris Market: "Osiris - Product - <name>"
        if title_text.startswith('Osiris -'):
            return "Osiris"

        # Pattern for Carthasis Market: "<name> - <vendor> - Catharsis"
        if title_text.endswith('- Catharsis') or ' - Catharsis' in title_text:
            return "Catharsis"

        # Pattern for Black Ops: "Product «...» - Black Ops" (plain hyphen, so the
        # generic em-dash/pipe splitters below miss it).
        if title_text.endswith('- Black Ops') or ' - Black Ops' in title_text:
            return "Black Ops"

        # Pattern for Drug Hub: "Drug Hub - Product Name"
        if title_text.startswith('Drug Hub - '):
            return "Drug Hub"

        # Look for pattern: "Product Name – Marketplace Name" (em dash)
        if '–' in title_text:
            parts = title_text.split('–')
            if len(parts) > 1:
                marketplace = clean_text(parts[-1])
                if marketplace:
                    return marketplace
        
        # Alternative pattern: "Product Name | Marketplace Name"
        if '|' in title_text:
            parts = title_text.split('|')
            if len(parts) > 1:
                marketplace = clean_text(parts[-1])
                if marketplace:
                    return marketplace
    
    # Try meta tags as fallback
    meta_site = soup.find('meta', attrs={'property': 'og:site_name'})
    if meta_site and meta_site.get('content'):
        return clean_text(meta_site['content'])
    
    # Try to find site name in headers or other elements
    site_selectors = [
        '.site-title',
        '.site-name',
        '.brand',
        '.logo',
        'h1[class*="site"]',
        'h1[class*="brand"]'
    ]
    
    for selector in site_selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            if text and len(text) < 50:  # Reasonable length for site name
                return text
    
    return ""


def extract_listing_title(soup):
    """Extract the product/listing title for each marketplace.

    Order matters: the market-specific selectors below are tried before the
    generic fallbacks. The earlier ``a.title`` / ``a[class*="title"]`` selectors
    were removed from the front because on the WooCommerce markets (Zion,
    SHADOWGATE, E-Market, Maria Shop, BestShop, ...) they matched a vendor or
    sidebar link instead of the real product title.
    """
    title_tag = soup.find('title')
    title_text = clean_text(title_tag.get_text()) if title_tag else ""

    # 1. Drug Hub: the product page <h1> is a "Shopping Cart" modal, so the real
    #    listing title only lives in <title> as "Drug Hub - <product>".
    if title_text.startswith('Drug Hub - '):
        product = clean_text(title_text[len('Drug Hub - '):])
        if product:
            return product

    # 1b. Osiris Market: title is "Osiris - Product - <name>"; the generic dash
    #     splitter below would otherwise cut it down to just "Osiris".
    if title_text.startswith('Osiris - Product - '):
        product = clean_text(title_text[len('Osiris - Product - '):])
        if product:
            return product

    # 1c. Abacus Market: every product-page <h1> is a sidebar heading ("About
    #     Vendor", "Listing Options", ...), so the generic h1 fallback grabs junk.
    #     The real title lives in <title> as "<name> | Abacus Market".
    if ' | Abacus Market' in title_text:
        product = clean_text(title_text.split(' | Abacus Market')[0])
        if product:
            return product

    # 2. Black Ops: custom template, title in a dedicated div (no h1). Checked
    #    before the generic title splitters because the title contains hyphens.
    blackops = soup.select_one('.product_pg_r_title')
    if blackops:
        text = clean_text(blackops.get_text())
        if text:
            return text

    # 3. WooCommerce product pages. This covers Zion, Emotive Drugstore,
    #    E-Market, BlackStar, Maria Shop, BestShop, SHADOWGATE, Darkweb
    #    Dispensary, Apex Chemicals, Grace Med Store, Moon Market (Docs) and
    #    Dark Market -- they all render the product name in the <h1> that
    #    carries the ``product_title`` class.
    woo_selectors = [
        'h1.product_title.entry-title',
        'h1.product-title.product_title.entry-title',
        'h1.product_title',
        'h1.product-title',
        'h1.entry-title.product-title',
        'h1.entry-title',
        'h1[class*="product"][class*="title"]',
    ]
    for selector in woo_selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            if text:
                return text

    # 4. TorZon: custom PHP market with no product_title h1. The product name is
    #    the lone <h5> inside the centered product cell.
    torzon = soup.select_one('center h5')
    if torzon:
        text = clean_text(torzon.get_text())
        if text:
            return text

    # 5. Generic fallbacks for any other / unknown marketplace.
    selectors = [
        'h1[class*="product"]',
        'h1[class*="title"]',
        'h1',
        '.product-title',
        '.product-name',
        '.drug-name',
        'title'
    ]

    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            if text and len(text) > 3:  # Avoid very short titles
                # Clean up common title patterns
                text = re.sub(r'\s*-\s*.*$', '', text)  # Remove everything after dash
                text = re.sub(r'\s*\|.*$', '', text)    # Remove everything after pipe
                text = re.sub(r'\s*Buy\s*', '', text, flags=re.IGNORECASE)  # Remove "Buy" prefix
                return clean_text(text)

    return ""


def extract_price(soup):
    """Extract price information from various possible selectors"""
    prices = []
    
    # Priority 1: Look in summary section first (for X Wave Market and similar WooCommerce sites)
    summary = soup.find(class_='summary')
    if summary:
        summary_price = summary.find(class_=lambda x: x and 'price' in str(x).lower())
        if summary_price:
            text = clean_text(summary_price.get_text())
            if text and '$' in text:
                price_matches = re.findall(r'\$[\d,]+\.?\d*', text)
                if price_matches:
                    # Filter out $0 if there are other prices
                    non_zero_prices = [p for p in price_matches if p != '$0']
                    if non_zero_prices:
                        if len(non_zero_prices) == 1:
                            return non_zero_prices[0]
                        else:
                            return f"{min(non_zero_prices)} - {max(non_zero_prices)}"
                    elif price_matches:
                        # If only $0 found, return it
                        return price_matches[0]
    
    # Priority 2: General price selectors
    selectors = [
        '.price .woocommerce-Price-amount',
        '.price .amount',
        '.price',
        '.product-price',
        '.drug-price',
        '[class*="price"]',
        '.woocommerce-Price-amount'
    ]
    
    for selector in selectors:
        elements = soup.select(selector)
        for element in elements:
            text = clean_text(element.get_text())
            if text and '$' in text:
                # Extract price values
                price_matches = re.findall(r'\$[\d,]+\.?\d*', text)
                prices.extend(price_matches)
    
    if prices:
        # Remove duplicates and filter out $0 prices if there are others
        unique_prices = list(set(prices))
        non_zero_prices = [p for p in unique_prices if p != '$0']
        
        if non_zero_prices:
            if len(non_zero_prices) == 1:
                return non_zero_prices[0]
            else:
                # Return price range
                return f"{min(non_zero_prices)} - {max(non_zero_prices)}"
        elif unique_prices:
            # If only $0 found, return it
            if len(unique_prices) == 1:
                return unique_prices[0]
            else:
                return f"{min(unique_prices)} - {max(unique_prices)}"
    
    return ""


def extract_dosage(soup):
    """Extract dosage information from tables and text"""
    dosage_info = []
    
    # Look for dosage in tables
    tables = soup.find_all('table')
    for table in tables:
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                for i, cell in enumerate(cells):
                    text = clean_text(cell.get_text())
                    # Look for dosage patterns
                    if re.search(r'\d+\s*mg', text, re.IGNORECASE) or re.search(r'\d+\s*ml', text, re.IGNORECASE):
                        dosage_info.append(text)
    
    # Look for dosage in text content
    text_content = soup.get_text()
    dosage_matches = re.findall(r'\d+\s*mg|\d+\s*ml|\d+\s*grams?|\d+\s*g', text_content, re.IGNORECASE)
    dosage_info.extend(dosage_matches)
    
    # Look for specific dosage selectors
    dosage_selectors = [
        '.dosage',
        '.strength',
        '.mg',
        '[class*="dosage"]',
        '[class*="strength"]'
    ]
    
    for selector in dosage_selectors:
        elements = soup.select(selector)
        for element in elements:
            text = clean_text(element.get_text())
            if text:
                dosage_info.append(text)
    
    # Clean and deduplicate
    if dosage_info:
        unique_dosages = list(set([clean_text(d) for d in dosage_info if clean_text(d)]))
        return " | ".join(unique_dosages[:3])  # Limit to 3 most relevant
    
    return ""


def extract_rating(soup):
    """Extract rating information if available"""
    rating_selectors = [
        '.rating',
        '.stars',
        '.review-rating',
        '.woocommerce-product-rating',
        '[class*="rating"]',
        '[class*="star"]'
    ]
    
    for selector in rating_selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            # Look for rating patterns like "4.5/5" or "4.5 stars"
            rating_match = re.search(r'(\d+\.?\d*)\s*/\s*5|(\d+\.?\d*)\s*stars?', text, re.IGNORECASE)
            if rating_match:
                return rating_match.group(1) or rating_match.group(2)
    
    return ""


def extract_reviews(soup):
    """Extract review information if available"""
    review_selectors = [
        '.reviews',
        '.customer-reviews',
        '.product-reviews',
        '.woocommerce-reviews',
        '[class*="review"]'
    ]
    
    reviews = []
    
    for selector in review_selectors:
        elements = soup.select(selector)
        for element in elements:
            # Look for individual review text
            review_texts = element.find_all(['p', 'div', 'span'], string=True)
            for review_text in review_texts:
                text = clean_text(review_text.get_text())
                if text and len(text) > 20:  # Only meaningful reviews
                    reviews.append(text)
    
    if reviews:
        return " | ".join(reviews[:2])  # Limit to 2 most relevant reviews
    
    return ""


def extract_description(soup):
    """Extract drug description from various possible locations"""
    # Black Ops keeps the description body in .product_pg_r_text. Check it first
    # with a low length gate -- these blurbs (active ingredient / manufacturer /
    # package) are short but are exactly what the keyword filter needs.
    blackops_desc = soup.select_one('.product_pg_r_text')
    if blackops_desc:
        text = clean_text(blackops_desc.get_text())
        if text and len(text) > 10:
            return text[:500] + "..." if len(text) > 500 else text

    description_selectors = [
        '.product-description',
        '.product-content',
        '.woocommerce-product-details__short-description',
        '.entry-content',
        '.product-summary',
        '.description',
        '[class*="description"]'
    ]
    
    for selector in description_selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            if text and len(text) > 50:  # Only meaningful descriptions
                # Limit description length
                if len(text) > 500:
                    text = text[:500] + "..."
                return text
    
    # Fallback: look for meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        text = clean_text(meta_desc['content'])
        if text and len(text) > 20:
            return text
    
    return ""


def extract_number_in_stocks(soup):
    """Extract the number of items in stock from various possible locations"""
    if not soup:
        return ""
    
    # Pattern 1: Look for text patterns like "20000 in stock", "20000 in-stock", etc.
    # Common patterns: "NUMBER in stock", "NUMBER available", "Stock: NUMBER", etc.
    stock_patterns = [
        r'(\d[\d,]*)\s+in\s+stock',
        r'(\d[\d,]*)\s+in-stock',
        r'(\d[\d,]*)\s+available',
        r'stock[:\s]+(\d[\d,]*)',
        r'quantity[:\s]+(\d[\d,]*)',
        r'available[:\s]+(\d[\d,]*)',
    ]
    
    # Get all text content
    text_content = soup.get_text()
    
    for pattern in stock_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            stock_number = match.group(1).replace(',', '').strip()
            if stock_number.isdigit():
                return stock_number
    
    # Pattern 2: Look for elements with stock-related classes
    stock_selectors = [
        '.stock',
        '.in-stock',
        '[class*="stock"]',
        '[class*="quantity"]',
        '.product-stock',
        '.stock-status',
        'p.stock',
        '.woocommerce-stock'
    ]
    
    for selector in stock_selectors:
        elements = soup.select(selector)
        for element in elements:
            text = clean_text(element.get_text())
            if text:
                # Look for numbers in the text
                for pattern in stock_patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        stock_number = match.group(1).replace(',', '').strip()
                        if stock_number.isdigit():
                            return stock_number
                
                # Also try to extract any number from the element text
                numbers = re.findall(r'(\d[\d,]*)', text)
                for num in numbers:
                    num_clean = num.replace(',', '').strip()
                    if num_clean.isdigit() and len(num_clean) > 0:
                        # Check if surrounding context suggests it's a stock number
                        if any(keyword in text.lower() for keyword in ['stock', 'available', 'quantity']):
                            return num_clean
    
    # Pattern 3: Check for data attributes that might contain stock info
    stock_attrs = soup.find_all(attrs={'data-stock': True})
    if stock_attrs:
        for elem in stock_attrs:
            stock_value = elem.get('data-stock', '')
            if stock_value and stock_value.isdigit():
                return stock_value
    
    stock_attrs = soup.find_all(attrs={'data-quantity': True})
    if stock_attrs:
        for elem in stock_attrs:
            quantity_value = elem.get('data-quantity', '')
            if quantity_value and quantity_value.isdigit():
                return quantity_value
    
    return ""


def is_product_url(url):
    """Return True only for individual product/listing detail pages.

    The scraper also captures non-listing pages -- add-to-cart redirects (which
    return the shop page), category/shop index pages and vendor storefronts.
    Those have no real product title, so we skip them at parse time. The rules
    below cover every market's product-URL shape:

      - Grace Med / BlackStar:  /?product=<slug>      (query string)
      - most WooCommerce shops:  /product/<slug>/
      - Emotive / Apex / SHADOWGATE:  /shop/<slug>/
      - Drug Hub:  /listing/<id>/...
      - Carthasis:  /item/<uuid>
      - Osiris:  /product/<uuid>  (covered by the /product/ rule above)
      - TorZon:  /products.php?action=view&...

    Anything with an ``add-to-cart`` param, a ``product_cat`` category listing,
    a /store/ or /market/ vendor page, or a bare /shop/ index is rejected.
    """
    if not url:
        return False

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # add-to-cart actions just re-render the shop page, never a product detail.
    if 'add-to-cart' in query:
        return False

    segments = [s for s in parsed.path.split('/') if s]

    # Query-string product pages (Grace Med, BlackStar). Guard against the
    # ?product_cat=... category listings which share the same host.
    if query.get('product') and not query.get('product_cat'):
        return True

    # /product/<slug> detail pages (most WooCommerce markets).
    if 'product' in segments and segments.index('product') + 1 < len(segments):
        return True

    # Drug Hub listing pages: /listing/<id>/...
    if 'listing' in segments:
        return True

    # Carthasis Market product pages: /item/<uuid>
    if 'item' in segments and segments.index('item') + 1 < len(segments):
        return True

    # TorZon custom PHP market: /products.php?action=view
    if parsed.path.endswith('products.php') and query.get('action') == ['view']:
        return True

    # /shop/<slug> detail pages (Emotive, Apex, SHADOWGATE). A bare /shop/ index
    # has no slug segment and is rejected.
    if segments and segments[0] == 'shop' and len(segments) > 1:
        return True

    return False


def parse_product_html(product_data):
    """Parse a single product's HTML and extract all relevant information"""
    try:
        html = product_data.get('html', '')
        if not html:
            return None
        
        soup = BeautifulSoup(html, 'html.parser')
        
        parsed_data = {
            "market_name": extract_market_name(soup),
            "listing_title": extract_listing_title(soup),
            "price": extract_price(soup),
            "dosage": extract_dosage(soup),
            "rating": extract_rating(soup),
            "review": extract_reviews(soup),
            "description": extract_description(soup),
            "number_in_stocks": extract_number_in_stocks(soup),
            "original_url": product_data.get('product_url', ''),
            "category_page": product_data.get('category_page', ''),
            "fetched_at": product_data.get('fetched_at', '')
        }
        
        return parsed_data
        
    except Exception as e:
        print(colored(f"❌ Error parsing product: {e}", "red"))
        return None


def load_products_data(filename="products_html.json"):
    """Load the scraped products data"""
    if not os.path.exists(filename):
        print(colored(f"❌ {filename} not found!", "red"))
        return []
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            print(colored(f"✅ Loaded {len(data)} products from {filename}", "green"))
            return data
    except Exception as e:
        print(colored(f"❌ Error loading {filename}: {e}", "red"))
        return []


def save_parsed_data(parsed_products, filename="../data/parsed_drugs.json"):
    """Save the parsed drug data to a new JSON file"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(parsed_products, f, ensure_ascii=False, indent=2)
        print(colored(f"✅ Saved {len(parsed_products)} parsed products to {filename}", "green"))
    except Exception as e:
        print(colored(f"❌ Error saving to {filename}: {e}", "red"))


def main():
    """Main function to parse all products and save results"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_input = os.path.join(base_dir, "data", "merged", "products_html_merged.json")
    default_output = os.path.join(base_dir, "data", "parsed", "parsed_merged.json")

    arg_parser = argparse.ArgumentParser(description="Parse scraped product HTML into structured records.")
    arg_parser.add_argument("--input", "-i", default=default_input,
                            help=f"Path to the (merged) products_html JSON (default: {default_input})")
    arg_parser.add_argument("--output", "-o", default=default_output,
                            help=f"Destination for parsed records (default: {default_output})")
    args = arg_parser.parse_args()

    print(colored("🚀 Starting HTML Parser for Drug Data", "cyan", attrs=['bold']))

    # Load the scraped data
    products_data = load_products_data(args.input)
    if not products_data:
        print(colored("❌ No data to parse. Exiting.", "red"))
        return
    
    parsed_products = []
    failed_count = 0
    skipped_count = 0

    print(colored(f"\n📊 Processing {len(products_data)} products...", "cyan"))

    for i, product_data in enumerate(products_data, 1):
        print(colored(f"  [{i}/{len(products_data)}] Processing...", "white"), end=" ")

        # Skip non-listing pages (add-to-cart redirects, category/shop indexes
        # and vendor storefronts) -- they carry no real product title.
        if not is_product_url(product_data.get('product_url', '')):
            skipped_count += 1
            print(colored("⏭️  (non-listing)", "yellow"))
            continue

        parsed_data = parse_product_html(product_data)

        if parsed_data:
            parsed_products.append(parsed_data)
            print(colored("✅", "green"))
        else:
            failed_count += 1
            print(colored("❌", "red"))
    
    # Save the parsed data
    print(colored(f"\n{'='*80}", "cyan"))
    print(colored(f"💾 SAVING RESULTS", "cyan", attrs=['bold']))
    print(colored(f"{'='*80}", "cyan"))
    
    save_parsed_data(parsed_products, args.output)

    print(colored(f"\n✅ Parsing complete!", "green", attrs=['bold']))
    print(colored(f"   Successfully parsed: {len(parsed_products)} products", "green"))
    print(colored(f"   Skipped (non-listing pages): {skipped_count}", "yellow"))
    print(colored(f"   Failed to parse: {failed_count} products", "red" if failed_count > 0 else "green"))
    print(colored(f"   Saved to: {args.output}", "green"))


if __name__ == "__main__":
    main()
