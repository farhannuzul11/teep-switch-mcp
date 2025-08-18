# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp[cli]",
#     "pyzabbix",
#     "python-dotenv",
# ]
# ///

import os
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pyzabbix import ZabbixAPI
from typing import List, Dict, Any, Optional
from typing import Optional, List, Dict, Any
import uvicorn
import time 
import json

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# Get Zabbix server URL and API token from environment variables
ZABBIX_URL = os.getenv("ZABBIX_SERVER_URL")
ZABBIX_TOKEN = os.getenv("ZABBIX_API_TOKEN")

# --- Initialize MCP Server ---
# Create a FastMCP instance for a stateless HTTP server, with a descriptive name
mcp = FastMCP("ZabbixInfoServer", stateless_http=True)

# --- Helper Function ---
def _get_zabbix_connection() -> ZabbixAPI:
    """Establish and return a connection to the Zabbix API."""
    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise ValueError("ZABBIX_SERVER_URL and ZABBIX_API_TOKEN environment variables must be set.")
    
    try:
        zapi = ZabbixAPI(ZABBIX_URL)
        zapi.login(api_token=ZABBIX_TOKEN)
        return zapi
    except Exception as e:
        # Raise ConnectionError for more specific error handling
        raise ConnectionError(f"Failed to connect to Zabbix API: {e}") from e

# --- Tool Definitions ---

@mcp.tool()
async def get_hosts(
    limit: int = 10,
    host_name_filter: Optional[str] = None,
    only_problematic: bool = False
) -> str:
    """
    Retrieve and format a list of monitored hosts from Zabbix.

    Returns:
        str: Formatted host list string.
    """
    # Limit the number of hosts returned to 15 max
    if limit > 15:
        limit = 15
    
    zapi = _get_zabbix_connection()
    
    # Prepare output fields; include 'name' if filtering by host name
    output_fields = ["hostid", "host"]
    if host_name_filter:
        output_fields.append("name")
    
    # Prepare parameters for API call
    params: Dict[str, Any] = {"output": output_fields, "limit": limit}
    
    # If filtering by host name, add search criteria
    if host_name_filter:
        params["search"] = {"name": host_name_filter}
        params["searchByAny"] = True

    # If only problematic hosts are requested, add filter
    if only_problematic:
        params["withProblems"] = True

    # Retrieve hosts from Zabbix API
    hosts = zapi.host.get(**params)

    # Return a message if no hosts found
    if not hosts:
        return "No hosts found on the Zabbix server."

    # Format the result as a clean multi-line string
    result_lines = [f"Found {len(hosts)} host(s) being monitored:\n"]
    for idx, host in enumerate(hosts, 1):
        result_lines.append(
            f"{idx}. Host ID: {host.get('hostid', 'N/A')}\n"
            f"   Host Name: {host.get('host', 'N/A')}\n"
        )

    return json.dumps(hosts) 


@mcp.tool()
async def get_host_details(host_name: str) -> Dict[str, Any]:
    """
    Retrieve concise and important details about a specific host from Zabbix.

    Args:
        host_name (str): Visible name of the host in Zabbix (e.g., "Webserver-Prod-01").

    Returns:
        Dict[str, Any]: Host object with key details including status, main interface, proxy, groups, and templates.

    Raises:
        ValueError: If host is not found.
    """
    zapi = _get_zabbix_connection()
    
    hosts = zapi.host.get(
        filter={"name": host_name},
        output=["hostid", "host", "status"],  # Basic info + status
        selectInterfaces=["ip", "port", "type", "main"],  # Only main interface details needed
        selectParentProxies=["proxyid", "host"],  # Proxy info
        selectGroups=["name"],  # Group names only
        selectTemplates=["name"]  # Template names only
    )
    
    if not hosts:
        raise ValueError(f"Host '{host_name}' not found.")
    
    return hosts[0]


@mcp.tool()
async def get_problems(
    limit: int = 5,
    min_severity: int = 0,
    time_period_seconds: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Retrieve unresolved problems from Zabbix, including associated host details.

    Args:
        limit (int): Max number of problems to return (max 15).
        min_severity (int): Minimum severity (0=Info to 5=Not classified).
        time_period_seconds (Optional[int]): Time window in seconds (max 86400).

    Returns:
        List[Dict[str, Any]]: Problems enriched with host name and ID.
    """
    if limit > 15:
        limit = 15
        print("Warning: limit reduced to 15.")

    max_period = 86400  # 24 hours
    if time_period_seconds and time_period_seconds > max_period:
        time_period_seconds = max_period
        print(f"Warning: time period reduced to {max_period} seconds (24h).")

    zapi = _get_zabbix_connection()
    params = {
        "output": "extend",
        "limit": limit,
        "sortfield": ["eventid"],
        "sortorder": "DESC",
        "min_severity": min_severity,
        "recent": True
    }
    if time_period_seconds:
        params["timefrom"] = int(time.time()) - time_period_seconds

    problems = zapi.problem.get(**params)

    for problem in problems:
        problem['associated_host_name'] = "Unknown Host"
        problem['associated_host_id'] = "N/A"

        if problem.get('object') == '0' and problem.get('objectid'):
            trigger_id = problem['objectid']
            try:
                triggers = zapi.trigger.get(
                    triggerids=trigger_id,
                    selectHosts=["hostid", "name"],
                    output="extend"
                )
                if triggers and triggers[0].get('hosts'):
                    host = triggers[0]['hosts'][0]
                    problem['associated_host_name'] = host.get('name', "Unknown Host")
                    problem['associated_host_id'] = host.get('hostid', "N/A")
            except Exception as e:
                print(f"Error fetching host for trigger {trigger_id}: {e}")
                problem['associated_host_name'] = f"Error fetching host: {e}"

    return problems


# --- Main Execution Block ---
if __name__ == "__main__":
    print(f"Starting ZabbixInfoServer MCP on http://localhost:3200")
    print("Ensure ZABBIX_SERVER_URL and ZABBIX_API_TOKEN are set in your .env file.")
    uvicorn.run(mcp.streamable_http_app, host="localhost", port=3200)
