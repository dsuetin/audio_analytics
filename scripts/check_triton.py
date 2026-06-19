import tritonclient.grpc as grpcclient
from pprint import pprint

client = grpcclient.InferenceServerClient("localhost:8002")

print("=== MODELS ===")
pprint(client.get_model_repository_index())

print("\n=== CONFIG ===")
pprint(
    client.get_model_config(
        "emformer_conformer_online_tdt_punct_microphone_v1",
        as_json=True,
    )
)