"""Triton model-control helper (gRPC :8001): load/unload/ready.

Usage:
  python3 triton_model_ctl.py load  <model> [repo]
  python3 triton_model_ctl.py unload <model> [repo]
  python3 triton_model_ctl.py ready  <model>
  python3 triton_model_ctl.py index
"""
import sys

import grpc
import tritonclient.grpc as grpc_client
from tritonclient.grpc import service_pb2, service_pb2_grpc

URL = "127.0.0.1:8001"
REPO = "./"


def main():
    action = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else None
    repo = sys.argv[3] if len(sys.argv) > 3 else REPO
    channel = grpc.insecure_channel(URL)
    stub = service_pb2_grpc.GRPCInferenceServiceStub(channel)

    if action == "index":
        resp = stub.RepositoryIndex(service_pb2.RepositoryIndexRequest())
        for m in resp.models:
            print(f"{m.name} state={m.state} version={m.version}")
    elif action == "load":
        stub.RepositoryModelLoad(
            service_pb2.RepositoryModelLoadRequest(model_name=model)
        )
        print("load request sent:", model)
    elif action == "unload":
        stub.RepositoryModelUnload(
            service_pb2.RepositoryModelUnloadRequest(model_name=model)
        )
        print("unload request sent:", model)
    elif action == "ready":
        resp = stub.ModelReady(service_pb2.ModelReadyRequest(name=model))
        print(model, "ready=", resp.ready)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
