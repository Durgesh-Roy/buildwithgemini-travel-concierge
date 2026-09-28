# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import datetime
import json
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from dotenv import load_dotenv
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors.agent_engine_sandbox_code_executor import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from app.a2ui_utils import a2ui_callback

load_dotenv()

# CRITICAL: Hardcode Project ID, Bucket Name, and Memory Bank ID strings
FIRESTORE_PROJECT = "qwiklabs-gcp-02-e9f0b61d58c6"
GCS_BUCKET_NAME = "bwg3-qwiklabs-gcp-02-e9f0b61d58c6"
MEMORY_BANK_ID = "8731483519936626688"
LOCATION = "us-east1"

# Load Agent Engine resource name from deployment_metadata.json if available
agent_engine_resource_name = None
metadata_path = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")
if os.path.exists(metadata_path):
    try:
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
            agent_engine_resource_name = metadata.get("remote_agent_runtime_id")
    except Exception:
        pass

code_executor = AgentEngineSandboxCodeExecutor(
    agent_engine_resource_name=agent_engine_resource_name
)


def memory_bank_service_builder():
    """Returns a VertexAiMemoryBankService configured for this app."""
    return VertexAiMemoryBankService(
        project=FIRESTORE_PROJECT,
        location=LOCATION,
        agent_engine_id=MEMORY_BANK_ID,
    )


async def generate_memories_callback(callback_context: CallbackContext):
    """Sends session events to Memory Bank for extraction after each turn."""
    await callback_context.add_session_to_memory()
    return None


def get_firestore_client() -> firestore.Client:
    """Initializes and returns a Firestore client pinned to the explicit project ID string."""
    return firestore.Client(project=FIRESTORE_PROJECT)


def get_maps_api_key() -> str:
    """Retrieve Google Maps API key from environment."""
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def search_destinations(query: str = "", category: str = "", max_budget: float = 0.0) -> list[dict]:
    """Search travel destinations stored in the Firestore database.

    Args:
        query: Optional location or keyword search string (e.g., 'Japan', 'Paris').
        category: Optional category filter (e.g., 'Cultural & Urban', 'Beach & Tropical').
        max_budget: Optional maximum average daily budget limit in USD.

    Returns:
        List of matching destination dictionaries.
    """
    db = get_firestore_client()
    docs = db.collection("destinations").stream()

    results = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id

        # Apply filtering
        if query and (
            query.lower() not in data.get("name", "").lower()
            and query.lower() not in data.get("country", "").lower()
            and query.lower() not in data.get("description", "").lower()
        ):
            continue
        if category and category.lower() not in data.get("category", "").lower():
            continue
        if max_budget > 0 and data.get("avg_cost_per_day", 0) > max_budget:
            continue

        results.append(data)

    return results


def get_destination_details(destination_id: str) -> dict:
    """Retrieve detailed information about a specific travel destination by its document ID.

    Args:
        destination_id: The document ID of the destination (e.g., 'tokyo-japan', 'paris-france').

    Returns:
        Destination details dictionary, or error message if not found.
    """
    db = get_firestore_client()
    doc = db.collection("destinations").document(destination_id).get()

    if not doc.exists:
        return {"error": f"Destination '{destination_id}' not found."}

    data = doc.to_dict()
    data["id"] = doc.id
    return data


def add_destination(
    name: str,
    country: str,
    category: str,
    description: str,
    avg_cost_per_day: float,
    best_season: str,
    highlights: list[str],
) -> dict:
    """Add a new travel destination to the Firestore catalog.

    Args:
        name: Name of the destination/city (e.g., 'Kyoto, Japan').
        country: Country name.
        category: Travel category (e.g., 'Cultural & Urban', 'Beach & Tropical').
        description: A brief summary of the destination.
        avg_cost_per_day: Estimated average daily cost in USD.
        best_season: Best season/months to visit.
        highlights: List of top attractions or activities.

    Returns:
        Status dictionary with the created destination ID.
    """
    doc_id = name.lower().replace(",", "").replace(" ", "-")
    data = {
        "id": doc_id,
        "name": name,
        "country": country,
        "category": category,
        "description": description,
        "avg_cost_per_day": float(avg_cost_per_day),
        "best_season": best_season,
        "highlights": highlights,
    }

    db = get_firestore_client()
    db.collection("destinations").document(doc_id).set(data)

    return {"status": "success", "message": f"Successfully added destination '{name}'.", "id": doc_id}


async def generate_destination_image(
    destination_name: str,
    description: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Generate a visual preview image for a travel destination using gemini-3.1-flash-lite-image in the global region, save as artifact, and upload to GCS.

    Args:
        destination_name: Name of the destination or attraction (e.g., 'Tokyo, Japan', 'Eiffel Tower').
        description: Optional additional visual details for generating the image.

    Returns:
        Dictionary containing public Cloud Storage URL and status.
    """
    try:
        genai_client = genai.Client(vertexai=True, project=FIRESTORE_PROJECT, location="global")

        prompt = f"A beautiful, high-quality scenic photograph of {destination_name}."
        if description:
            prompt += f" {description}"

        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )

        image_bytes = None
        mime_type = "image/jpeg"
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    image_bytes = part.inline_data.data
                    if part.inline_data.mime_type:
                        mime_type = part.inline_data.mime_type
                    break

        if not image_bytes:
            return {"error": f"Failed to generate image bytes for destination '{destination_name}'."}

        filename = f"{destination_name.lower().replace(',', '').replace(' ', '-')}.jpg"

        if tool_context:
            artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            await tool_context.save_artifact(filename, artifact_part)

        storage_client = storage.Client(project=FIRESTORE_PROJECT)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        object_name = f"destinations/{filename}"
        blob = bucket.blob(object_name)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{object_name}"

        return {
            "destination": destination_name,
            "filename": filename,
            "public_url": public_url,
            "status": "success",
        }
    except Exception as e:
        return {"error": f"Image generation failed: {str(e)}"}


async def generate_destination_video(
    destination_name: str,
    prompt_details: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Generate a short video preview for a travel destination using Google's Omni model (gemini-omni-flash-preview) in the global region, save as an artifact, and upload to GCS.

    Args:
        destination_name: Name of the destination or attraction (e.g., 'Tokyo Shibuya Crossing', 'Bali Resort').
        prompt_details: Optional additional specific visual details for generating the video.

    Returns:
        Dictionary containing public Cloud Storage URL, filename, and status.
    """
    try:
        genai_client = genai.Client(vertexai=True, project=FIRESTORE_PROJECT, location="global")

        prompt = f"A short, high-quality scenic video clip of {destination_name}."
        if prompt_details:
            prompt += f" {prompt_details}"

        interaction = genai_client.interactions.create(
            model="gemini-omni-flash-preview",
            input=prompt,
        )

        video_bytes = None
        mime_type = "video/mp4"

        if hasattr(interaction, "output_video") and interaction.output_video:
            data = getattr(interaction.output_video, "data", None)
            if data:
                if isinstance(data, str):
                    video_bytes = base64.b64decode(data)
                elif isinstance(data, (bytes, bytearray)):
                    video_bytes = bytes(data)

        if not video_bytes and hasattr(interaction, "steps") and interaction.steps:
            for step in interaction.steps:
                if getattr(step, "type", None) == "model_output" and getattr(step, "content", None):
                    for content in step.content:
                        if getattr(content, "type", None) == "video" or getattr(content, "mime_type", "").startswith("video/"):
                            data = getattr(content, "data", None)
                            if data:
                                video_bytes = base64.b64decode(data) if isinstance(data, str) else bytes(data)
                                if getattr(content, "mime_type", None):
                                    mime_type = content.mime_type
                                break

        if not video_bytes:
            return {"error": f"Failed to generate video bytes for destination '{destination_name}'."}

        filename = f"{destination_name.lower().replace(',', '').replace(' ', '-')}.mp4"

        if tool_context:
            artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
            await tool_context.save_artifact(filename, artifact_part)

        storage_client = storage.Client(project=FIRESTORE_PROJECT)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        object_name = f"videos/{filename}"
        blob = bucket.blob(object_name)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{object_name}"

        return {
            "destination": destination_name,
            "filename": filename,
            "public_url": public_url,
            "status": "success",
        }
    except Exception as e:
        return {"error": f"Video generation failed: {str(e)}"}


def fetch_live_weather(location: str) -> dict:
    """Fetch real-time live weather conditions and temperature for any city worldwide using Open-Meteo.

    Args:
        location: City or location name (e.g. 'Tokyo', 'Paris', 'New York').

    Returns:
        Dictionary containing real-time temperature (Celsius and Fahrenheit), wind speed, and location info.
    """
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(location)}&count=1"
        req_g = urllib.request.Request(geo_url, headers={"User-Agent": "ADKAgent/1.0"})
        with urllib.request.urlopen(req_g) as resp_g:
            geo_data = json.loads(resp_g.read().decode())

        if not geo_data.get("results"):
            return {"error": f"Could not find coordinates for location '{location}'."}

        loc = geo_data["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        city_name = loc.get("name", location)
        country = loc.get("country", "")

        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        req_w = urllib.request.Request(weather_url, headers={"User-Agent": "ADKAgent/1.0"})
        with urllib.request.urlopen(req_w) as resp_w:
            weather_data = json.loads(resp_w.read().decode())

        current = weather_data.get("current_weather", {})
        temp_c = current.get("temperature")
        temp_f = round((temp_c * 9 / 5) + 32, 1) if temp_c is not None else None

        return {
            "location": f"{city_name}, {country}".strip(", "),
            "temperature_c": temp_c,
            "temperature_f": temp_f,
            "windspeed_kmh": current.get("windspeed"),
            "time": current.get("time"),
        }
    except Exception as e:
        return {"error": f"Failed to fetch live weather: {str(e)}"}


def convert_currency(amount: float, from_currency: str = "USD", to_currency: str = "EUR") -> dict:
    """Convert an amount between currencies (e.g., USD to JPY, EUR, GBP) using the Frankfurter Public API.

    Args:
        amount: The monetary amount to convert (e.g. 150.0).
        from_currency: 3-letter currency code to convert from (default 'USD').
        to_currency: 3-letter currency code to convert to (e.g. 'JPY', 'EUR', 'GBP').

    Returns:
        Dictionary containing the conversion result, rate, date, and original amount.
    """
    try:
        from_curr = from_currency.upper().strip()
        to_curr = to_currency.upper().strip()

        if from_curr == to_curr:
            return {
                "original_amount": amount,
                "from_currency": from_curr,
                "converted_amount": amount,
                "to_currency": to_curr,
                "rate": 1.0,
            }

        url = f"https://api.frankfurter.app/latest?amount={amount}&from={from_curr}&to={to_curr}"
        req = urllib.request.Request(url, headers={"User-Agent": "ADKAgent/1.0"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())

        rates = data.get("rates", {})
        converted_val = rates.get(to_curr)
        if converted_val is None:
            return {"error": f"Could not convert from {from_curr} to {to_curr}."}

        unit_rate = round(converted_val / amount, 4) if amount > 0 else 0.0
        return {
            "original_amount": amount,
            "from_currency": from_curr,
            "converted_amount": round(converted_val, 2),
            "to_currency": to_curr,
            "rate": unit_rate,
            "date": data.get("date"),
        }
    except Exception as e:
        return {"error": f"Currency conversion failed: {str(e)}"}


def geocode_address(address: str) -> dict:
    """Turn a street address or location name into geographic coordinates using Google Geocoding API.

    Args:
        address: Address or place name (e.g., 'Eiffel Tower, Paris', 'Shinjuku, Tokyo').

    Returns:
        Dictionary containing formatted address, latitude, longitude, and location details.
    """
    api_key = get_maps_api_key()
    if not api_key or api_key == "PASTE_KEY_HERE":
        try:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(address)}&count=1"
            req = urllib.request.Request(geo_url, headers={"User-Agent": "ADKAgent/1.0"})
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
            if data.get("results"):
                res = data["results"][0]
                return {
                    "formatted_address": f"{res.get('name')}, {res.get('country')}".strip(", "),
                    "location": {"latitude": res.get("latitude"), "longitude": res.get("longitude")},
                    "note": "Using fallback geocoding service (add valid GOOGLE_MAPS_API_KEY to .env for Google Maps)",
                }
        except Exception:
            pass
        return {"error": "GOOGLE_MAPS_API_KEY is not set or valid in .env."}

    try:
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(address)}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") != "OK" or not data.get("results"):
            return {"error": f"Geocoding failed: {data.get('error_message', data.get('status'))}"}

        result = data["results"][0]
        location = result["geometry"]["location"]
        return {
            "formatted_address": result.get("formatted_address"),
            "location": {"latitude": location.get("lat"), "longitude": location.get("lng")},
        }
    except Exception as e:
        return {"error": f"Geocoding request failed: {str(e)}"}


def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "tourist_attraction",
    radius: float = 1000.0,
    max_results: int = 5,
) -> dict:
    """Find nearby places of a given type around coordinates using Google Places API (New).

    Args:
        latitude: Center latitude coordinate.
        longitude: Center longitude coordinate.
        place_type: Place type to search for (e.g., 'restaurant', 'tourist_attraction', 'museum', 'hotel').
        radius: Search radius in meters (default 1000.0).
        max_results: Maximum number of places to return (1 to 20).

    Returns:
        Dictionary containing a list of nearby places with name, address, and location coordinates.
    """
    api_key = get_maps_api_key()
    if not api_key or api_key == "PASTE_KEY_HERE":
        return {"error": "GOOGLE_MAPS_API_KEY is set to placeholder in .env. Please update with your Google Maps API key."}

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
    }
    body = {
        "includedTypes": [place_type],
        "maxResultCount": min(max(1, max_results), 20),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(latitude), "longitude": float(longitude)},
                "radius": float(radius),
            }
        },
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())

        raw_places = data.get("places", [])
        formatted_places = []
        for p in raw_places:
            display_name = p.get("displayName", {}).get("text", "Unknown")
            address = p.get("formattedAddress", "")
            loc = p.get("location", {})
            formatted_places.append({
                "name": display_name,
                "address": address,
                "location": {"latitude": loc.get("latitude"), "longitude": loc.get("longitude")},
            })

        return {"places": formatted_places, "count": len(formatted_places)}
    except Exception as e:
        return {"error": f"Places search failed: {str(e)}"}


def get_current_time(query: str) -> str:
    """Simulates getting current time for a city.

    Args:
        query: City name.

    Returns:
        Time string.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are Travel Concierge, a helpful AI assistant that provides expert travel advice, "
        "recommends destinations, manages a travel catalog, generates destination preview images, "
        "fetches live weather, converts currency, looks up places and coordinates, and can safely "
        "execute Python code in a sandbox. You pay strict attention to remembering and honoring all user "
        "allergies (food, environmental, or medical) and dietary restrictions across conversations to "
        "ensure user safety, filter out unsuitable destinations/restaurants, and personalize all responses."
    ),
    workflow_description="Analyze the request and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        "{\"Image\": {\"url\": {\"literalString\": \"https://...\"}}}. Never point an "
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=instruction,
    code_executor=code_executor,
    tools=[
        PreloadMemoryTool(),
        search_destinations,
        get_destination_details,
        add_destination,
        generate_destination_image,
        generate_destination_video,
        fetch_live_weather,
        convert_currency,
        geocode_address,
        find_nearby_places,
        get_current_time,
    ],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
