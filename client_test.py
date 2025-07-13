from src.abstract.config_container import ConfigContainer

def test_load_config():
    config = ConfigContainer.form_file("examples/server.json")
    for name, server in config.items():
        print(f"Server name: {name}")
        print(f"Server config: {server}\n")

if __name__ == "__main__":
    test_load_config()
