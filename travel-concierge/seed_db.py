"""Seed script for travel-concierge Firestore database."""

from google.cloud import firestore

# CRITICAL: Hardcode Project ID string (not project number)
FIRESTORE_PROJECT = "qwiklabs-gcp-02-e9f0b61d58c6"

DESTINATIONS = [
    {
        "id": "tokyo-japan",
        "name": "Tokyo, Japan",
        "country": "Japan",
        "category": "Cultural & Urban",
        "description": "A vibrant metropolis blending ultra-modern tech with historic shinto temples and incredible culinary scenes.",
        "avg_cost_per_day": 180,
        "best_season": "Spring & Autumn",
        "highlights": ["Shinjuku Nightlife", "Senso-ji Temple", "Tsukiji Outer Market"],
    },
    {
        "id": "kyoto-japan",
        "name": "Kyoto, Japan",
        "country": "Japan",
        "category": "Historical & Nature",
        "description": "Japan's ancient capital renowned for classical Buddhist temples, gardens, imperial palaces, and traditional wooden houses.",
        "avg_cost_per_day": 150,
        "best_season": "Spring & Autumn",
        "highlights": ["Fushimi Inari Taisha", "Arashiyama Bamboo Grove", "Kinkaku-ji Golden Pavilion"],
    },
    {
        "id": "paris-france",
        "name": "Paris, France",
        "country": "France",
        "category": "Romantic & Arts",
        "description": "The City of Light, global center for art, fashion, gastronomy, and iconic architectural landmarks.",
        "avg_cost_per_day": 220,
        "best_season": "Spring & Summer",
        "highlights": ["Eiffel Tower", "Louvre Museum", "Montmartre & Sacré-Cœur"],
    },
    {
        "id": "bali-indonesia",
        "name": "Bali, Indonesia",
        "country": "Indonesia",
        "category": "Beach & Tropical",
        "description": "An Indonesian island known for iconic rice paddies, volcanic mountains, beaches, and coral reefs.",
        "avg_cost_per_day": 90,
        "best_season": "Dry Season (April - October)",
        "highlights": ["Ubud Monkey Forest", "Uluwatu Temple Sunset", "Tegallalang Rice Terraces"],
    },
]

def seed():
    print(f"Connecting to Firestore for project: {FIRESTORE_PROJECT}...")
    db = firestore.Client(project=FIRESTORE_PROJECT)
    collection_ref = db.collection("destinations")

    for dest in DESTINATIONS:
        doc_id = dest["id"]
        collection_ref.document(doc_id).set(dest)
        print(f"  ✓ Seeded destination: {dest['name']} ({doc_id})")

    print("Firestore seeding complete!")

if __name__ == "__main__":
    seed()
