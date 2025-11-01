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
- Consider removing data processing (eg. Splitting, stripping, etc) to store raw data
"""

import json
import re
import os
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
    """Extract listing title from various h1 selectors for different marketplaces"""
    # Try various h1 selectors for different marketplaces
    h1_selectors = [
        'h1.product_title.entry-title',  # Emotive drugstore
        'h1.product-title.entry-title',  # X Wave Market
        'h1.entry-title.product-title',  # X Wave Market (alternate order)
        'h1.entry-title',                # X Wave Market, general
        'h1.product_title',
        'h1.product-title',
        'h1[class*="product"][class*="title"]',
    ]
    
    for selector in h1_selectors:
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text())
            if text:
                return text
    
    # Fallback to other selectors for other marketplaces
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


def save_parsed_data(parsed_products, filename="parsed_drugs.json"):
    """Save the parsed drug data to a new JSON file"""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(parsed_products, f, ensure_ascii=False, indent=2)
        print(colored(f"✅ Saved {len(parsed_products)} parsed products to {filename}", "green"))
    except Exception as e:
        print(colored(f"❌ Error saving to {filename}: {e}", "red"))


def main():
    """Main function to parse all products and save results"""
    print(colored("🚀 Starting HTML Parser for Drug Data", "cyan", attrs=['bold']))
    
    # Load the scraped data
    products_data = load_products_data()
    if not products_data:
        print(colored("❌ No data to parse. Exiting.", "red"))
        return
    
    parsed_products = []
    failed_count = 0
    
    print(colored(f"\n📊 Processing {len(products_data)} products...", "cyan"))
    
    for i, product_data in enumerate(products_data, 1):
        print(colored(f"  [{i}/{len(products_data)}] Processing...", "white"), end=" ")
        
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
    
    save_parsed_data(parsed_products)
    
    print(colored(f"\n✅ Parsing complete!", "green", attrs=['bold']))
    print(colored(f"   Successfully parsed: {len(parsed_products)} products", "green"))
    print(colored(f"   Failed to parse: {failed_count} products", "red" if failed_count > 0 else "green"))
    print(colored(f"   Saved to: parsed_drugs.json", "green"))


if __name__ == "__main__":
    main()
