import sys
print(f"Using Python: {sys.executable}")
try:
    from phoenix.client import Client
    print("Phoenix Client import successful!")
except ImportError as e:
    print(f"Import failed: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}") 