import json
from typing import Self
from pydantic import BaseModel, HttpUrl
from mcp import StdioServerParameters


class HTTPServerParameters(BaseModel):
    url: HttpUrl
    opts: dict | None = None


class ConfigContainer(BaseModel): #BaseModel
    stdio: dict[str, StdioServerParameters] = {}
    http: dict[str, HTTPServerParameters] = {}

    @classmethod
    def form_file(cls, file_path: str) -> Self:    
        try:
            with open(file_path, "r") as file:
                json_data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise ValueError(f"Error reading file: {e}") 

        try:
            return cls(**json_data)
        except Exception as e:
            raise ValueError(f"Error processing configuration: {e}")
        
