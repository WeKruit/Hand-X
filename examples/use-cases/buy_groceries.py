import asyncio
from browser_use import Agent, Browser, ChatBrowserUse
from model import GroceryCart
from typing import List

async def add_to_cart(items: List[str] = ["milk", "eggs", "bread"]):
    browser = Browser(
        cdp_url="http://localhost:9222"
    )

    llm = ChatBrowserUse()

    # Task prompt
    task = f"""
    Search for "{items}" on Instacart at the nearest store.

    You will buy all of the items at the same store.
    For each item:
    1. Search for the item
    2. Find the best match (closest name, lowest price)
    3. Add the item to the cart

    Site:
    - Instacart: https://www.instacart.com/
    """

    # Create agent with structured output
    agent = Agent(
        browser=browser,
        llm=llm,
        task=task,
        output_model_schema=GroceryCart,
    )

    # Run the agent
    result = await agent.run()
    return result

if __name__ == "__main__":
    # Get user input
    items_input = input("What items would you like to add to cart (comma-separated)? ").strip()
    if not items_input:
        items = ["milk", "eggs", "bread"]
        print(f"Using default items: {items}")
    else:
        items = [item.strip() for item in items_input.split(",")]

    result = asyncio.run(add_to_cart(items))

    # Access structured output
    if result and result.structured_output:
        cart = result.structured_output

        print(f"\n{'='*60}")
        print(f"Items Added to Cart")
        print(f"{'='*60}\n")

        for item in cart.items:
            print(f"Name: {item.name}")
            print(f"Price: ${item.price}")
            if item.brand:
                print(f"Brand: {item.brand}")
            if item.size:
                print(f"Size: {item.size}")
            print(f"URL: {item.url}")
            print(f"{'-'*60}")
