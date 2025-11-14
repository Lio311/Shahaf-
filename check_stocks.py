import pandas as pd
import yfinance as yf
import smtplib
import os
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
from datetime import datetime
import traceback
import requests # For Gemini
import json # For Gemini

# --- Configuration ---
PORTFOLIO_FILE = 'תיק מניות.xlsx'
TICKER_COLUMN = 'טיקר'
BUY_PRICE_COLUMN = 'מחיר עלות'
SHARES_COLUMN = 'כמות מניות'
HEADER_ROW = 8 # The row where data starts (headers are row 9)
# ----------------------
# --- Email Configuration (Reads from Environment Variables) ---
SENDER_EMAIL = os.environ.get('GMAIL_USER')
SENDER_PASSWORD = os.environ.get('GMAIL_PASSWORD')
RECIPIENT_EMAIL = os.environ.get('RECIPIENT_EMAIL')
# --- Gemini API Key ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# ----------------------

def clean_price(price_str):
    """ Cleans a price string, removing currency symbols or other non-numeric characters. """
    if isinstance(price_str, (int, float)):
        return float(price_str)
    if not isinstance(price_str, str):
        price_str = str(price_str)
    
    cleaned_str = re.sub(r"[^0-9.-]", "", price_str)
    
    try:
        return float(cleaned_str) if cleaned_str else None
    except ValueError:
        print(f"Warning: Could not convert price string '{price_str}' to float.")
        return None

def clean_quantity(qty_str):
    """ Cleans a quantity string/number to ensure it's a float. """
    if isinstance(qty_str, (int, float)):
        return float(qty_str)
    if not isinstance(qty_str, str):
        qty_str = str(qty_str)
        
    cleaned_str = re.sub(r"[^0-9.]", "", qty_str) # Only allow digits and a dot
    
    try:
        return float(cleaned_str) if cleaned_str else 0.0
    except ValueError:
        print(f"Warning: Could not convert quantity string '{qty_str}' to float. Defaulting to 0.")
        return 0.0

def get_sp500_tickers():
    """ Fetches the list of S&P 500 tickers from Wikipedia. """
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        # Add headers to avoid 403 error
        tables = pd.read_html(
            url,
            storage_options={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        sp500_table = tables[0]
        tickers = sp500_table['Symbol'].tolist()
        # Clean tickers (remove dots that Wikipedia uses)
        tickers = [ticker.replace('.', '-') for ticker in tickers]
        return tickers
    except Exception as e:
        print(f"Error fetching S&P 500 tickers: {e}")
        return []

def get_nasdaq100_tickers():
    """ Fetches the list of NASDAQ-100 tickers from Wikipedia. """
    try:
        url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        # Add headers to avoid 403 error
        tables = pd.read_html(
            url,
            storage_options={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        nasdaq_table = tables[4] # The correct table index
        tickers = nasdaq_table['Ticker'].tolist()
        return tickers
    except Exception as e:
        print(f"Error fetching NASDAQ-100 tickers: {e}")
        return []

def get_general_market_movers():
    """ Scans S&P 500 and NASDAQ-100 stocks for big movers (both losers and gainers). """
    print("\nScanning market for big movers...")
    
    # Get all major index tickers
    print("Fetching S&P 500 tickers...")
    sp500 = get_sp500_tickers()
    print(f"Got {len(sp500)} S&P 500 tickers")
    
    print("Fetching NASDAQ-100 tickers...")
    nasdaq100 = get_nasdaq100_tickers()
    print(f"Got {len(nasdaq100)} NASDAQ-100 tickers")
    
    # Combine and remove duplicates
    all_tickers = list(set(sp500 + nasdaq100))
    print(f"Total unique tickers to scan: {len(all_tickers)}")
    
    if not all_tickers:
        print("Could not fetch any tickers.")
        return [], []
    
    try:
        # Download data in batches to avoid rate limits
        batch_size = 100
        all_movers = []
        
        for i in range(0, len(all_tickers), batch_size):
            batch = all_tickers[i:i+batch_size]
            print(f"Processing batch {i//batch_size + 1}/{(len(all_tickers)-1)//batch_size + 1} ({len(batch)} tickers)...")
            
            try:
                data = yf.download(batch, period="2d", progress=False, auto_adjust=False, threads=True)
                
                if data.empty or len(data) < 2:
                    continue
                
                close_prices = data['Close']
                latest_prices = close_prices.iloc[-1]
                prev_prices = close_prices.iloc[-2]
                
                for ticker in batch:
                    try:
                        if len(batch) == 1:
                            current = latest_prices
                            previous = prev_prices
                        else:
                            current = latest_prices.get(ticker)
                            previous = prev_prices.get(ticker)
                        
                        if current is None or previous is None or pd.isna(current) or pd.isna(previous):
                            continue
                        
                        pct_change = ((current - previous) / previous) * 100
                        
                        # Collect all significant movers (>5% up or down)
                        if abs(pct_change) >= 5.0:
                            all_movers.append({
                                'ticker': ticker,
                                'current': float(current),
                                'previous': float(previous),
                                'pct_change': float(pct_change)
                            })
                    except Exception as e:
                        continue
                        
            except Exception as e:
                print(f"Error processing batch: {e}")
                continue
        
        if not all_movers:
            print("No stocks found with moves over 5%.")
            return [], []
        
        # Separate losers and gainers
        losers = [m for m in all_movers if m['pct_change'] < 0]
        gainers = [m for m in all_movers if m['pct_change'] > 0]
        
        print(f"\nFound {len(losers)} stocks down >5% and {len(gainers)} stocks up >5%.")
        print("Fetching market cap data...")
        
        # Process losers
        final_losers = []
        for item in losers:
            try:
                ticker_obj = yf.Ticker(item['ticker'])
                info = ticker_obj.info
                market_cap = info.get('marketCap', 0)
                
                # Filter: Market cap over 100M
                if market_cap > 100_000_000:
                    final_losers.append({
                        'Symbol': item['ticker'],
                        'Name': info.get('shortName', item['ticker']),
                        '% Change': item['pct_change'],
                        'Market Cap': f"${market_cap/1e9:.1f}B" if market_cap > 1e9 else f"${market_cap/1e6:.0f}M"
                    })
            except Exception as e:
                continue
        
        # Process gainers
        final_gainers = []
        for item in gainers:
            try:
                ticker_obj = yf.Ticker(item['ticker'])
                info = ticker_obj.info
                market_cap = info.get('marketCap', 0)
                
                # Filter: Market cap over 100M
                if market_cap > 100_000_000:
                    final_gainers.append({
                        'Symbol': item['ticker'],
                        'Name': info.get('shortName', item['ticker']),
                        '% Change': item['pct_change'],
                        'Market Cap': f"${market_cap/1e9:.1f}B" if market_cap > 1e9 else f"${market_cap/1e6:.0f}M"
                    })
            except Exception as e:
                continue
        
        # Sort and take top 20 of each
        final_losers_sorted = sorted(final_losers, key=lambda x: x['% Change'])[:20]
        final_gainers_sorted = sorted(final_gainers, key=lambda x: x['% Change'], reverse=True)[:20]
        
        print(f"Final: {len(final_losers_sorted)} losers and {len(final_gainers_sorted)} gainers (Cap > 100M).")
        return final_losers_sorted, final_gainers_sorted
        
    except Exception as e:
        print(f"Error in get_general_market_movers: {e}")
        traceback.print_exc()
        return [], []

def get_gemini_analysis(portfolio_details, general_market_losers, general_market_gainers, total_daily_p_l_ils):
    """
    Sends portfolio data to Gemini API for analysis and returns an HTML-formatted summary.
    **MODIFIED FOR HEBREW OUTPUT**
    """
    
    if not GEMINI_API_KEY:
        print("Gemini API key not found. Skipping AI analysis.")
        return "<p><i>(AI analysis is not configured. Please add a GEMINI_API_KEY secret.)</i></p>"

    # 1. Create the prompt with portfolio and market data
    prompt_data = f"My portfolio's total gain/loss for today is ₪{total_daily_p_l_ils:+.2f}.\n\nHere is my detailed portfolio data (in ILS):\n"
    for item in portfolio_details:
        prompt_data += (
            f"- {item['ticker']} ({item['num_shares']} shares): "
            f"Total P/L: {item['total_p_l']:+.2f}₪ ({item['total_change_pct']:.1f}%), "
            f"Daily P/L: {item['daily_p_l']:+.2f}₪ ({item['daily_change_pct']:.1f}%)\n"
        )
    
    prompt_data += "\nHere are today's top market gainers (Cap > 100M, Up > 5%):\n"
    for item in general_market_gainers:
        prompt_data += f"- {item['Symbol']} ({item['Name']}): {item['% Change']:.1f}%\n"
    prompt_data += "\nHere are today's top market losers (Cap > 100M, Drop > 5%):\n"
    for item in general_market_losers:
        prompt_data += f"- {item['Symbol']} ({item['Name']}): {item['% Change']:.1f}%\n"
    
    # --- MODIFIED SYSTEM INSTRUCTION ---
    system_instruction = (
        "You are a financial analyst. Your task is to provide a brief, high-level summary of the provided data **in Hebrew**. "
        "**Do NOT give financial advice, recommendations, or price predictions.** "
        "Just summarize the key facts **in Hebrew**. "
        "Start with a 1-sentence summary (in Hebrew) of the portfolio's total daily P/L (in ₪). "
        "Then, add 1-2 sentences (in Hebrew) about specific portfolio stocks with significant movements (mentioning P/L ₪ amounts). "
        "Finally, add a 1-sentence comment (in Hebrew) on the general market scan. "
        "Keep the entire response to 3-4 sentences total, **all in Hebrew**."
    )
    # --- END OF MODIFICATION ---

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt_data}]
        }],
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        }
    }

    try:
        response = requests.post(api_url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=20)
        response.raise_for_status() # Will raise an error for bad status codes
        
        result = response.json()
        
        if 'candidates' in result and result['candidates']:
            text = result['candidates'][0]['content']['parts'][0]['text']
            
            # Perform the replace operation *before* the f-string
            formatted_text = text.replace('\n', '<br>')
            html_output = f"<p>{formatted_text}</p>"
            
            html_output += "<p style='font-size: 0.7em; color: #666; font-style: italic;'><b>הבהרה:</b> סיכום זה נוצר על ידי AI והוא למטרות מידע בלבד ואינו מהווה ייעוץ פיננסי.</p>"
            return html_output
        else:
            print("Gemini API returned no candidates.")
            return "<p><i>(AI analysis returned no response.)</i></p>"

    except requests.exceptions.RequestException as e:
        print(f"Error calling Gemini API: {e}")
        return f"<p><i>(Error fetching AI analysis: {e})</i></p>"
    except Exception as e:
        print(f"Error processing Gemini response: {e}")
        traceback.print_exc()
        return "<p><i>(Error processing AI analysis.)</i></p>"

def get_gemini_insights(portfolio_details, general_market_losers, general_market_gainers, total_daily_p_l_ils):
    """
    Sends data to Gemini API for high-level insights.
    **MODIFIED FOR HEBREW OUTPUT**
    """
    
    if not GEMINI_API_KEY:
        return "<p><i>(AI analysis is not configured.)</i></p>"

    # 1. Create the prompt data with English labels (data is data)
    prompt_data = f"My portfolio's total daily P/L: ₪{total_daily_p_l_ils:+.2f}.\n\n"
    prompt_data += "My portfolio details (in ILS):\n"
    for item in portfolio_details:
        prompt_data += (
            f"- {item['ticker']} ({item['num_shares']} shares): "
            f"Total P/L: ₪{item['total_p_l']:+.2f} ({item['total_change_pct']:.1f}%), "
            f"Daily P/L: ₪{item['daily_p_l']:+.2f} ({item['daily_change_pct']:.1f}%)\n"
        )
    
    prompt_data += "\nToday's Top Market Losers (Cap > 100M, Drop > 5%):\n"
    for item in general_market_losers:
        prompt_data += f"- {item['Symbol']} ({item['Name']}): {item['% Change']:.1f}% (Market Cap: {item['Market Cap']})\n"
    prompt_data += "\nToday's Top Market Gainers (Cap > 100M, Up > 5%):\n"
    for item in general_market_gainers:
        prompt_data += f"- {item['Symbol']} ({item['Name']}): {item['% Change']:.1f}% (Market Cap: {item['Market Cap']})\n"

    # 2. Create the System Instruction in **HEBREW**
    
    # --- MODIFIED SYSTEM INSTRUCTION ---
    system_instruction = (
        "You are a financial analyst. Your task is to identify interesting risks and opportunities in the provided data. "
        "Your analysis is based on the provided price, P/L, and market cap data and You have access to news or fundamental data."
        "\n\n"
        "**Crucially: You must NOT give specific buy or sell recommendations (e.g., 'You should buy X' or 'You should sell Y').**"
        "\n\n"
        "Instead, provide 2-3 'points for thought' in **HEBREW**, as bullet points."
        "Focus on: "
        "1. Identifying a stock from the user's portfolio that had a sharp move (up or down) and what they should check about it (in Hebrew)."
        "2. Identifying a stock from the 'Top Losers' list that might be an 'interesting opportunity for further research' (e.g., a large-cap stock with a sharp drop) (in Hebrew)."
        "3. A general insight about the portfolio's performance relative to the market (in Hebrew)."
        "\n\n"
        "The response MUST be in **HEBREW**."
    )
    # --- END OF MODIFICATION ---

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt_data}]
        }],
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        }
    }

    try:
        response = requests.post(api_url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        
        result = response.json()
        
        if 'candidates' in result and result['candidates']:
            text = result['candidates'][0]['content']['parts'][0]['text']
            
            # --- Formatting fix starts here ---
            # 1. Convert Markdown bold (**text**) to HTML bold (<b>text</b>)
            formatted_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
            # ---------------------
            
            # 2. Convert Markdown bullets (*) to HTML list items (<li>)
            # (Use the already-fixed formatted_text, not the original text)
            formatted_text = formatted_text.replace('* ', '<li>')
            
            # Handle both \n and <br> from the model
            formatted_text = re.sub(r'\n|<br>', '</li>', formatted_text)
            
            # Clean up potential empty list items
            formatted_text = re.sub(r'<li>\s*</li>', '', formatted_text)
            
            # Ensure everything is wrapped in <ul>
            if '<li>' in formatted_text:
                if not formatted_text.endswith('</li>'):
                        formatted_text += '</li>'
                html_output = f"<ul>{formatted_text}</ul>"
            else:
                # Fallback if no list was created
                html_output = f"<p>{formatted_text.replace('</li>', '<br>')}</p>"
            
            html_output += "<p style='font-size: 0.7em; color: #666; font-style: italic;'><b>הבהרה:</b> ניתוח AI זה מיועד למטרות מידע בלבד ואינו מהווה ייעוץ פיננסי. בצע מחקר משלך לפני קבלת החלטות.</p>"
            return html_output
        else:
            print("Gemini API (Insights) returned no candidates.")
            return "<p><i>(AI analysis returned no response.)</i></p>"

    except requests.exceptions.RequestException as e:
        print(f"Error calling Gemini API (Insights): {e}")
        return f"<p><i>(Error fetching AI insights: {e})</i></p>"
    except Exception as e:
        print(f"Error processing Gemini (Insights) response: {e}")
        traceback.print_exc()
        return "<p><i>(Error processing AI insights.)</i></p>"



def generate_html_report(portfolio_details, general_market_losers, general_market_gainers, gemini_analysis_html, gemini_insights_html, total_daily_p_l_ils):
    """ 
    Generates a complete HTML report string. 
    **MODIFIED FOR HEBREW TITLES AND RTL AI SECTIONS**
    """
    today = datetime.now().strftime("%B %d, %Y")
    
    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f7f7f7; padding: 20px; direction: ltr; }}
            h1 {{ color: #333; border-bottom: 2px solid #4CAF50; }}
            h2 {{ color: #444; margin-top: 30px; }}
            /* Style for the Total P/L summary */
            .total-pl-summary {{
                font-size: 1.5em;
                font-weight: bold;
                text-align: center;
                margin: 20px 0;
                padding: 15px;
                border-radius: 8px;
            }}
            .total-pl-positive {{ background-color: #e6f7ec; color: #2a874d; }}
            .total-pl-negative {{ background-color: #fdecea; color: #d9534f; }}
            
            table {{ border-collapse: collapse; width: 100%; margin-top: 15px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
            th {{ background-color: #4CAF50; color: white; }}
            tr:nth-child(even) {{ background-color: #f2f2f2; }}
            tr:hover {{ background-color: #e9e9e9; }}
            .positive {{ color: green; font-weight: bold; }}
            .negative {{ color: red; font-weight: bold; }}
            .neutral {{ color: #555; }}
            .alert-section {{ background-color: #fff0f0; border: 2px solid #d9534f; padding: 15px; border-radius: 8px; margin-top: 20px; }}
            .info-section {{ background-color: #f0f8ff; border: 2px solid #4a90e2; padding: 15px; border-radius: 8px; margin-top: 20px; }}
            .success-section {{ background-color: #f0fff4; border: 2px solid #48bb78; padding: 15px; border-radius: 8px; margin-top: 20px; }}
            
            /* --- MODIFIED Gemini Section Style --- */
            .gemini-section {{ 
                background-color: #fdf8e2; 
                border: 2px solid #f0b90b; 
                padding: 15px; 
                border-radius: 8px; 
                margin-top: 20px; 
                direction: rtl; /* Added */
                text-align: right; /* Added */
            }}
            .gemini-section h2 {{ margin-top: 0; color: #d98c00; }}
            .gemini-section p {{ font-size: 1.1em; line-height: 1.6; }}
            
            /* --- MODIFIED Insights Section Style --- */
            .insights-section {{
                background-color: #f3f0ff;
                border: 2px solid #6c48bb;
                padding: 15px;
                border-radius: 8px;
                margin-top: 20px;
                direction: rtl; /* Changed from ltr */
                text-align: right; /* Changed from left */
            }}
            .insights-section h2 {{ margin-top: 0; color: #5a3e9b; }}
            .insights-section ul {{ padding-left: 0; padding-right: 20px; }} /* Changed padding for RTL list */
            .insights-section li {{ font-size: 1.1em; line-height: 1.6; margin-bottom: 10px; }}
            
            .alert-section h2 {{ margin-top: 0; color: #d9534f; }}
            .info-section h2 {{ margin-top: 0; color: #4a90e2; }}
            .success-section h2 {{ margin-top: 0; color: #48bb78; }}
        </style>
    </head>
    <body>
        <h1 style="text-align: right; direction: rtl;">דוח מניות יומי - {today}</h1>

    <div class='gemini-section'>
        <h2>🤖 סיכום פיננסי - AI</h2>
        {gemini_analysis_html}
    </div>

    <div class='insights-section'>
        <h2>💡 תובנות אנליסט - AI</h2>
        {gemini_insights_html}
    </div>
    """
    
    # Personal Alerts Section
    total_drops = [s for s in portfolio_details if s['total_change_pct'] <= -30]
    daily_drops_10 = [s for s in portfolio_details if s['daily_change_pct'] <= -10]
    daily_drops_20 = [s for s in portfolio_details if s['daily_change_pct'] <= -20]
    daily_gains_20 = [s for s in portfolio_details if s['daily_change_pct'] >= 20]

    if daily_gains_20:
        html += "<div class='info-section'><h2>🚀 My Portfolio Movers (Up %)</h2>"
        html += "<h3 style='color:green;'>Stocks Up More Than 20% Today</h3><table>"
        html += "<tr><th>Stock</th><th>Daily Change</th></tr>"
        for s in daily_gains_20:
            html += f"<tr><td>{s['ticker']}</td><td class='positive'>{s['daily_change_pct']:.1f}%</td></tr>"
        html += "</table></div>"

    if total_drops or daily_drops_10 or daily_drops_20:
        html += "<div class='alert-section'><h2>🔻 My Portfolio Alerts & Drops</h2>"
        
        if total_drops:
            html += "<h3 style='color:#d9534f;'>TOTAL DROP Over 30%</h3><table>"
            html += "<tr><th>Stock</th><th>Buy Price</th><th>Current</th><th>Total Change</th></tr>"
            for s in total_drops:
                html += f"<tr><td>{s['ticker']}</td><td>{s['buy_price']:.2f}</td><td>{s['current_price']:.2f}</td><td class='negative'>{s['total_change_pct']:.1f}%</td></tr>"
            html += "</table>"
        if daily_drops_10:
            html += "<h3 style='color:#d9534f;'>⚠️ DAILY DROP Over 10%</h3><table>"
            html += "<tr><th>Stock</th><th>Yesterday</th><th>Current</th><th>Daily Change</th></tr>"
            for s in daily_drops_10:
                html += f"<tr><td>{s['ticker']}</td><td>{s['prev_close']:.2f}</td><td>{s['current_price']:.2f}</td><td class='negative'>{s['daily_change_pct']:.1f}%</td></tr>"
            html += "</table>"
        if daily_drops_20:
            html += "<h3 style='color:#d9534f;'>Stocks Down More Than 20% Today</h3><table>"
            html += "<tr><th>Stock</th><th>Daily Change</th></tr>"
            for s in daily_drops_20:
                html += f"<tr><td>{s['ticker']}</td><td class='negative'>{s['daily_change_pct']:.1f}%</td></tr>"
            html += "</table>"
            
        html += "</div>"
    
    # General Market Gainers Section
    if general_market_gainers:
        html += "<div class='success-section'>"
        html += "<h2>📈 General Market Scan - Top Gainers (Cap >100M, Up >5%)</h2>"
        html += "<table><tr><th>Stock</th><th>Name</th><th>Daily Change</th><th>Market Cap</th></tr>"
        for stock in general_market_gainers:
            html += f"""
                <tr>
                    <td>{stock['Symbol']}</td>
                    <td>{stock['Name']}</td>
                    <td class='positive'>{stock['% Change']:.2f}%</td>
                    <td>{stock['Market Cap']}</td>
                </tr>
            """
        html += "</table></div>"
    
    # General Market Losers Section
    html += "<div class='alert-section'>"
    html += "<h2>📉 General Market Scan - Top Losers (Cap >100M, Drop >5%)</h2>"
    
    if general_market_losers:
        html += "<table><tr><th>Stock</th><th>Name</th><th>Daily Change</th><th>Market Cap</th></tr>"
        for stock in general_market_losers:
            html += f"""
                <tr>
                    <td>{stock['Symbol']}</td>
                    <td>{stock['Name']}</td>
                    <td class='negative'>{stock['% Change']:.2f}%</td>
                    <td>{stock['Market Cap']}</td>
                </tr>
            """
        html += "</table>"
    else:
        html += "<p>No stocks found matching the criteria (Market Cap > 100M and Daily Drop > 5%).</p>"
    
    html += "</div>"

    # My Portfolio Summary
    html += "<h2>My Portfolio Summary</h2>"
    
    # Total Daily P/L Summary (in ILS ₪)
    total_pl_class = "total-pl-positive" if total_daily_p_l_ils >= 0 else "total-pl-negative"
    html += f"""
    <div class='total-pl-summary {total_pl_class}'>
        Today's Portfolio P/L: {total_daily_p_l_ils:+.2f}₪
    </div>
    """

    html += """
        <table>
            <tr>
                <th>Stock</th>
                <th>Shares</th>
                <th>Buy Price</th>
                <th>Current Price</th>
                <th>Daily P/L (₪)</th>
                <th>Daily Change (%)</th>
                <th>Total P/L (₪)</th>
                <th>Total Change (%)</th>
            </tr>
    """

    # Portfolio Summary Table (in ILS ₪)
    for stock in portfolio_details:
        daily_cls = "positive" if stock['daily_change_pct'] > 0 else ("negative" if stock['daily_change_pct'] < 0 else "neutral")
        total_cls = "positive" if stock['total_change_pct'] > 0 else ("negative" if stock['total_change_pct'] < 0 else "neutral")
        
        html += f"""
            <tr>
                <td>{stock['ticker']}</td>
                <td>{stock['num_shares']}</td>
                <td>{stock['buy_price']:.2f}</td>
                <td>{stock['current_price']:.2f}</td>
                <td class='{daily_cls}'>₪{stock['daily_p_l']:+.2f}</td>
                <td class='{daily_cls}'>{stock['daily_change_pct']:+.2f}%</td>
                <td class='{total_cls}'>₪{stock['total_p_l']:+.2f}</td>
                <td class='{total_cls}'>{stock['total_change_pct']:+.2f}%</td>
            </tr>
        """
    
    html += "</table>"
    html += "</body></html>"
    return html

def send_email(html_body):
    """ Sends an email with the given HTML body. """
    if not SENDER_EMAIL or not SENDER_PASSWORD or not RECIPIENT_EMAIL:
        print("Error: Email credentials (GMAIL_USER, GMAIL_PASSWORD, RECIPIENT_EMAIL) not set in environment variables.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    
    msg["Subject"] = f"📈 Daily Stock Report (with AI Summary & Insights) - {today}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            print("Daily report email sent successfully.")
    except Exception as e:
        print(f"Error sending email: {e}")
        traceback.print_exc()

def check_portfolio_and_report():
    try:
        df = pd.read_excel(PORTFOLIO_FILE, header=HEADER_ROW)
    except FileNotFoundError:
        print(f"Error: Could not find file {PORTFOLIO_FILE}")
        return
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        return

    df.columns = [str(c).strip() for c in df.columns]

    required_cols = [TICKER_COLUMN, BUY_PRICE_COLUMN, SHARES_COLUMN]
    for col in required_cols:
        if col not in df.columns:
            print(f"Error: Missing column '{col}'. Found columns: {list(df.columns)}")
            return

    # Fetch USD/ILS Exchange Rate
    print("Fetching USD/ILS exchange rate...")
    usd_ils_rate = 0.0
    try:
        ils_ticker = yf.Ticker("ILS=X")
        ils_data = ils_ticker.history(period="1d")
        if not ils_data.empty:
            usd_ils_rate = ils_data['Close'].iloc[-1]
            print(f"Current USD/ILS rate: {usd_ils_rate:.4f}")
        else:
            print("Warning: Could not fetch USD/ILS rate. Defaulting to 0. P/L will be incorrect.")
    except Exception as e:
        print(f"Error fetching USD/ILS rate: {e}. Defaulting to 0. P/L will be incorrect.")
    
    if usd_ils_rate == 0.0:
        # Fallback in case the rate fetch fails
        print("Using a fallback rate of 3.7. THIS IS A FALLBACK.")
        usd_ils_rate = 3.7 # Hardcoded fallback

    print("Reading portfolio from Excel...")
    
    # portfolio_map now holds a dictionary
    portfolio_map = {}
    for index, row in df.iterrows():
        ticker_symbol = str(row[TICKER_COLUMN]).strip()
        buy_price_raw = row[BUY_PRICE_COLUMN]
        shares_raw = row[SHARES_COLUMN]

        if not ticker_symbol or ticker_symbol.lower() == 'nan' or pd.isna(buy_price_raw) or pd.isna(shares_raw):
            continue
            
        buy_price = clean_price(buy_price_raw)
        num_shares = clean_quantity(shares_raw)
        
        if buy_price and num_shares > 0:
            portfolio_map[ticker_symbol] = {
                "buy_price": buy_price,
                "shares": num_shares
            }
    
    if not portfolio_map:
        print("No valid tickers with shares found in portfolio file.")
    
    tickers_list = list(portfolio_map.keys())
    
    # Portfolio Data Processing
    portfolio_details = []
    total_portfolio_daily_p_l_ils = 0.0 # Initialize total P/L in ILS
    
    if tickers_list:
        print(f"Fetching data for {len(tickers_list)} tickers: {', '.join(tickers_list)}")
        try:
            all_data = yf.download(tickers_list, period="2d", progress=False, auto_adjust=False)
            
            if all_data.empty or len(all_data) < 2:
                print("Could not download sufficient portfolio data from yfinance.")
            else:
                close_prices = all_data['Close']
                latest_prices = close_prices.iloc[-1]
                prev_prices = close_prices.iloc[-2]

                for ticker, data in portfolio_map.items():
                    try:
                        buy_price = data['buy_price']
                        num_shares = data['shares']
                        
                        if len(tickers_list) == 1:
                            # Handle case of single ticker download
                            current_price = latest_prices
                            prev_close = prev_prices
                        else:
                            current_price = latest_prices.get(ticker)
                            prev_close = prev_prices.get(ticker)

                        if current_price is None or prev_close is None or pd.isna(current_price) or pd.isna(prev_close):
                            print(f"Skipping {ticker}: Missing current or previous price data.")
                            continue

                        # P/L Calculations (USD)
                        daily_change_per_share = current_price - prev_close
                        total_change_per_share = current_price - buy_price
                        
                        daily_p_l_usd = daily_change_per_share * num_shares
                        total_p_l_usd = total_change_per_share * num_shares
                        
                        # Convert P/L to ILS
                        daily_p_l_ils = daily_p_l_usd * usd_ils_rate
                        total_p_l_ils = total_p_l_usd * usd_ils_rate
                        
                        total_portfolio_daily_p_l_ils += daily_p_l_ils # Add to total
                        
                        # Standard % Calculations
                        total_change_pct = (total_change_per_share / buy_price) * 100 if buy_price != 0 else 0
                        daily_change_pct = (daily_change_per_share / prev_close) * 100 if prev_close != 0 else 0

                        details = {
                            "ticker": ticker,
                            "buy_price": buy_price,
                            "current_price": current_price,
                            "prev_close": prev_close,
                            "daily_change_pct": daily_change_pct,
                            "total_change_pct": total_change_pct,
                            "num_shares": num_shares,
                            "daily_p_l": daily_p_l_ils, # Storing ILS value
                            "total_p_l": total_p_l_ils # Storing ILS value
                        }
                        portfolio_details.append(details)
                        
                        # Print statement now shows ILS
                        print(f"{ticker} ({num_shares} shares): Buy=${buy_price:.2f}, Current=${current_price:.2f}, "
                              f"Daily P/L=₪{daily_p_l_ils:+.2f} ({daily_change_pct:+.1f}%), "
                              f"Total P/L=₪{total_p_l_ils:+.2f} ({total_change_pct:+.1f}%)")
                    except KeyError:
                        print(f"Warning: No data found for ticker '{ticker}' in downloaded batch. It might be delisted or invalid.")
                    except Exception as e:
                        print(f"Error processing {ticker}: {e}")
                        traceback.print_exc()
        except Exception as e:
            print(f"Error downloading batch data from yfinance: {e}")
            traceback.print_exc()
    else:
        print("No tickers in portfolio file. Skipping portfolio processing.")

    # Get General Market Movers (both losers and gainers)
    general_market_losers, general_market_gainers = get_general_market_movers()

    # Get Gemini AI Analysis
    gemini_analysis_html = get_gemini_analysis(portfolio_details, general_market_losers, general_market_gainers, total_portfolio_daily_p_l_ils)
    
    gemini_insights_html = get_gemini_insights(portfolio_details, general_market_losers, general_market_gainers, total_portfolio_daily_p_l_ils)

    if not portfolio_details and not general_market_losers and not general_market_gainers:
        print("No portfolio details or general market movers to report.")
        return

    # Report Generation and Sending
    print("\nGenerating HTML report...")
    
    html_report = generate_html_report(
        portfolio_details,
        general_market_losers,
        general_market_gainers,
        gemini_analysis_html,
        gemini_insights_html, 
        total_portfolio_daily_p_l_ils
    )
    
    report_filename = "daily_stock_report.html"
    try:
        with open(report_filename, "w", encoding="utf-8") as f:
            f.write(html_report)
        print(f"✅ Report generated successfully: {report_filename}")
    except Exception as e:
        print(f"Error saving HTML file: {e}")

    # Send Email
    if SENDER_EMAIL and RECIPIENT_EMAIL:
        print("Sending email...")
        send_email(html_report)
    else:
        print("\nEmail credentials not set. Skipping email send.")
        print("View your report at: daily_stock_report.html")

if __name__ == "__main__":
    check_portfolio_and_report()
