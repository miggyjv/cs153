import os
from mistralai import Mistral
import discord
from dotenv import load_dotenv
import json
import time

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

FORMAT YOUR RESPONSE EXACTLY AS FOLLOWS:
- Rating: [Your rating] 
- Core factual assertions: [List numbered assertions]
- Evaluation of each assertion: [Evaluation]
- Research related information: [Research]
- Reasoning: [2-3 sentence explanation]
- Sources: [List bulleted sources]

Be concise and focused. Do not repeat the rating multiple times or create redundant sections."""

class FactCheckAgent:
    def __init__(self):
        MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
        self.client = Mistral(api_key=MISTRAL_API_KEY)
        
    async def fact_check(self, claim):
        """Analyze a claim and determine its factual accuracy."""
        
        # First, search for relevant information if we need to
        search_results = self.search_relevant_info(claim)
        
        messages = [
            {"role": "system", "content": FACT_CHECK_SYSTEM_PROMPT},
            {"role": "user", "content": f"Please fact check this claim: '{claim}'\n\nHere is some relevant information that might help: {search_results}"}
        ]
        
        response = await self.client.chat.complete_async(
            model=MISTRAL_MODEL,
            messages=messages,
        )
        
        # Get the raw result
        raw_result = response.choices[0].message.content
        
        # Create an embed for Discord
        embed = self.create_fact_check_embed(raw_result, claim)
        
        return embed
    
    def search_relevant_info(self, claim):
        """Optional: Search for information related to the claim."""
        # This is a placeholder for a real search implementation
        # You could use Google Search API, DuckDuckGo, or a similar service
        
        # For now, returning an empty string
        return "No additional information retrieved. Using model's built-in knowledge."
    
    async def run_discord(self, message: discord.Message):
        """Process a Discord message for fact checking."""
        # Extract the claim from the message
        # This assumes the bot is triggered by replying to a message to check
        if message.reference and message.reference.resolved:
            claim = message.reference.resolved.content
            result = await self.fact_check(claim)
            return result
        else:
            return "Please reply to a message containing a claim to fact check."

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
