import os
from mistralai import Mistral
import discord
from dotenv import load_dotenv
import json
import time
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options
import logging
import asyncio
import concurrent.futures

# No longer get discord's logger
# logger = logging.getLogger('discord')

MISTRAL_MODEL = "mistral-large-latest"
load_dotenv()

# Define the system prompt for fact checking
FACT_CHECK_SYSTEM_PROMPT = """You are an expert fact-checker. When presented with a claim:

1. Identify 3-7 core factual assertions being made (no more than 7)
2. Evaluate each assertion for accuracy 
3. Provide related information that helps verify or refute the claim
4. Rate the overall claim as: True, Partially True, False, or Unverifiable
5. Provide brief reasoning for your rating in 2-3 sentences
6. Include relevant sources

IMPORTANT RULES ABOUT SOURCES:
- DO NOT include any URLs or links in your sources
- Only cite Snopes if a relevant fact-check was found and provided to you
- For other sources, simply list the name of the source (e.g., "Official records", "News reports", "Expert testimony")
- If no specific sources are available, state "No specific sources available"

FORMAT YOUR RESPONSE EXACTLY AS FOLLOWS:
- Rating: [Your rating] 
- Core factual assertions: [List numbered assertions]
- Evaluation of each assertion: [Evaluation]
- Research related information: [Research]
- Reasoning: [2-3 sentence explanation]
- Sources: [List bulleted sources]

Be concise and focused. Do not repeat the rating multiple times or create redundant sections."""

# Add a thread pool executor for running WebDriver operations
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

class FactCheckAgent:
    def __init__(self, app_logger=None):
        """Initialize the FactCheckAgent with both Mistral and Chrome WebDriver."""
        # Use provided logger or create a default one
        self.logger = app_logger or logging.getLogger('fact_check_agent')
        
        MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
        self.client = Mistral(api_key=MISTRAL_API_KEY)
        
        # Set up Chrome WebDriver
        chrome_options = Options()
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        chrome_options.headless = True
        
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(options=chrome_options)
        
    async def fact_check(self, claim, request_id=None):
        """Analyze a claim and determine its factual accuracy."""
        
        log_prefix = f"[Request: {request_id}] " if request_id else ""
        
        # First, get a concise summary for searching
        self.logger.info(f"{log_prefix}Summarizing claim")
        summary = await self.summarize_claim(claim)
        cleaned_summary = self.clean_for_search(summary)
        
        # Run the Snopes search in a separate thread to avoid blocking the event loop
        self.logger.info(f"{log_prefix}Starting Snopes search for: {cleaned_summary}")
        loop = asyncio.get_event_loop()
        try:
            snopes_results = await loop.run_in_executor(
                executor, 
                self.search_relevant_info,
                cleaned_summary
            )
            self.logger.info(f"{log_prefix}Completed Snopes search successfully")
        except Exception as e:
            self.logger.error(f"{log_prefix}Error during Snopes search: {e}", exc_info=True)
            snopes_results = "No relevant Snopes fact-checks found. Using model's built-in knowledge."
        
        # Continue with fact check using Mistral
        self.logger.info(f"{log_prefix}Starting Mistral fact-check API call")
        messages = [
            {"role": "system", "content": FACT_CHECK_SYSTEM_PROMPT},
            {"role": "user", "content": f"""Please fact check this claim: '{claim}'

Here is what Snopes says about this or similar claims:
{snopes_results}

Please consider this information in your fact-check analysis."""}
        ]
        
        response = await self.client.chat.complete_async(
            model=MISTRAL_MODEL,
            messages=messages,
        )
        
        # Get the raw result
        raw_result = response.choices[0].message.content
        self.logger.info(f"{log_prefix}Received response from Mistral")
        
        # Create an embed for Discord
        embed = self.create_fact_check_embed(raw_result, claim)
        
        # Add Snopes reference if found
        if "No relevant Snopes fact-checks found" not in snopes_results:
            embed.add_field(
                name="Snopes Reference",
                value="✓ A related fact-check was found on Snopes and incorporated into this analysis.",
                inline=False
            )
        
        return embed
    
    def search_relevant_info(self, claim):
        """Search Snopes for information related to the claim."""
        try:
            # Use the Snopes search URL
            base_url = "https://www.snopes.com/search/"
            final_url = base_url + claim
            self.driver.get(final_url)
            
            # Look for fact-check links
            xpath_expression = "//a[starts-with(@href, 'https://www.snopes.com/fact-check/')]"
            wait = WebDriverWait(self.driver, 10)
            element = wait.until(EC.presence_of_element_located((By.XPATH, xpath_expression)))
            
            # Click the first fact-check result
            element.click()
            
            # Get the rating container
            container_xpath = '//*[@id="fact_check_rating_container"]'
            wait = WebDriverWait(self.driver, 10)
            container = wait.until(EC.presence_of_element_located((By.XPATH, container_xpath)))
            
            # Get the rating and explanation
            snopes_result = container.text
            
            return f"Snopes fact check result: {snopes_result}"
            
        except Exception as e:
            self.logger.warning(f"Error searching Snopes: {e}")
            return "No relevant Snopes fact-checks found. Using model's built-in knowledge."
            
    def create_fact_check_embed(self, raw_result, claim):
        """Create a Discord embed for the fact check result."""
        import discord
        
        # Determine rating and color - search the entire response
        rating = "Unverifiable"
        color = discord.Color.light_gray()  # Default color
        
        # Look for explicit rating section
        if "Rating:" in raw_result:
            if "False" in raw_result.split("Rating:")[1].split("\n")[0]:
                rating = "False"
                color = discord.Color.red()
            elif "True" in raw_result.split("Rating:")[1].split("\n")[0] and not "Partially True" in raw_result.split("Rating:")[1].split("\n")[0]:
                rating = "True"
                color = discord.Color.green()
            elif "Partially True" in raw_result.split("Rating:")[1].split("\n")[0]:
                rating = "Partially True"
                color = discord.Color.gold()
        # Fallback to searching entire text
        elif "False" in raw_result:
            rating = "False"
            color = discord.Color.red()
        elif "True" in raw_result and not "Partially True" in raw_result:
            rating = "True"
            color = discord.Color.green()
        elif "Partially True" in raw_result:
            rating = "Partially True"
            color = discord.Color.gold()
        
        # Create the embed with the appropriate color
        embed = discord.Embed(
            title="Fact Check Result",
            description=f"**Claim:** \"{truncate_text(claim, 4000)}\"",
            color=color
        )
        
        # Add the rating with an emoji
        emoji = "❓"
        if rating == "False":
            emoji = "❌"
        elif rating == "True":
            emoji = "✅"
        elif rating == "Partially True":
            emoji = "⚠️"
        
        embed.add_field(name="Rating", value=f"{emoji} {rating}", inline=False)
        
        # Parse the raw result into sections
        sections = {
            "Core factual assertions": "",
            "Evaluation of each assertion": "",
            "Research related information": "",
            "Explanation with evidence": "",
            "Sources": ""
        }
        
        current_section = None
        for line in raw_result.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            # Check if this is a section header
            for section_name in sections.keys():
                if section_name.lower() in line.lower() and ":" in line:
                    current_section = section_name
                    break
                
            if current_section and line and not any(section_name.lower() in line.lower() for section_name in sections.keys()):
                sections[current_section] += line + "\n"
        
        # Add each section as a field in the embed - with length checks
        for section_name, content in sections.items():
            if content.strip():
                # Split content into chunks if needed
                chunks = split_into_chunks(content.strip(), 1000)  # Slightly below 1024 for safety
                
                for i, chunk in enumerate(chunks):
                    field_name = section_name if i == 0 else f"{section_name} (continued)"
                    embed.add_field(name=field_name, value=chunk, inline=False)
        
        # Add footer with source links if available
        if "http" in sections["Sources"]:
            embed.set_footer(text="Sources included - click the links above for more information")
        
        return embed

    async def summarize_claim(self, claim):
        """Summarize a lengthy claim into 1-2 concise sentences for Snopes searching."""
        
        messages = [
            {"role": "system", "content": """You are a claim summarizer for fact-checking. 
Your task is to:
1. Extract the core assertion(s) from lengthy claims
2. Rewrite them in 3-5 words which include the person's full name and the time period
3. Focus on the key verifiable elements
4. Use language similar to how fact-checking sites phrase claims
5. Remove unnecessary details while preserving the main point
6. For pop culture claims, keep relevant names, dates, and specific details

Example input: "During the 1996 NBA Finals between the Chicago Bulls and Seattle SuperSonics, Michael Jordan was actually benched for Game 4 due to a secret suspension following a gambling scandal that the NBA covered up..."

Example output: "Michael Jordan gambling scandal"

Respond ONLY with the summarized claim, nothing else."""},
            {"role": "user", "content": f"Please summarize this claim: {claim}"}
        ]
        
        try:
            response = await self.client.chat.complete_async(
                model=MISTRAL_MODEL,
                messages=messages,
            )
            
            summary = response.choices[0].message.content.strip()
            return summary
            
        except Exception as e:
            self.logger.error(f"Error summarizing claim: {e}")
            return claim  # Return original claim if summarization fails

    def clean_for_search(self, text):
        """Clean text for search queries by removing special characters and extra spaces."""
        # Remove special characters but keep apostrophes for names
        cleaned = re.sub(r'[^a-zA-Z0-9\s\']', ' ', text)
        # Remove extra whitespace
        cleaned = ' '.join(cleaned.split())
        return cleaned

    def __del__(self):
        """Cleanup method to close the browser when the agent is destroyed."""
        try:
            self.driver.quit()
        except:
            pass

def truncate_text(text, max_length):
    """Truncate text to max_length characters."""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def split_into_chunks(text, chunk_size):
    """Split text into chunks of approximately chunk_size characters."""
    if len(text) <= chunk_size:
        return [text]
    
    chunks = []
    current_chunk = ""
    
    # Try to split at sentence boundaries when possible
    sentences = text.split('. ')
    
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 2 <= chunk_size:
            if current_chunk:
                current_chunk += ". " + sentence
            else:
                current_chunk = sentence
        else:
            # If this sentence would push us over the limit
            if current_chunk:
                chunks.append(current_chunk + ".")
                current_chunk = sentence
            else:
                # If a single sentence is longer than chunk_size
                if len(sentence) > chunk_size:
                    # Split the sentence at word boundaries
                    words = sentence.split()
                    current_chunk = ""
                    for word in words:
                        if len(current_chunk) + len(word) + 1 <= chunk_size:
                            if current_chunk:
                                current_chunk += " " + word
                            else:
                                current_chunk = word
                        else:
                            chunks.append(current_chunk)
                            current_chunk = word
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""
                else:
                    current_chunk = sentence
    
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks
