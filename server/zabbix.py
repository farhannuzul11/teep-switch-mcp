# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp[cli]",
#     "pyzabbix",
#     "python-dotenv",
# ]
# ///

## it to http server stateless

import os
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pyzabbix import ZabbixAPI
from typing import List, Dict, Any, Optional
import uvicorn
import time 

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
    """Establishes and returns a connection to the Zabbix API."""
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
) -> List[Dict[str, Any]]:
    """
    Retrieves a list of monitored hosts from Zabbix.

    Args:
        limit (int): The maximum number of hosts to return. Defaults to 10. Maximum is 15.
        host_name_filter (Optional[str]): Filters hosts by their visible name (case-sensitive partial match).
        only_problematic (bool): If True, only retrieves hosts that currently have problems.
                                 Note: This does not sort by "last problematic", but by current problem status.

    Returns:
        List[Dict[str, Any]]: A list of host objects, each containing hostid, name, and host.
    """
    if limit > 15:
        limit = 15
        print(f"Warning: Host limit reduced to 15 as it exceeds the maximum allowed.")
    
    zapi = _get_zabbix_connection()
    params: Dict[str, Any] = {"output": ["hostid", "name", "host"], "limit": limit}
    
    if host_name_filter:
        params["search"] = {"name": host_name_filter}
        params["searchByAny"] = True

    if only_problematic:
        params["withProblems"] = True # Filter for hosts that currently have problems

    hosts = zapi.host.get(**params)
    return hosts

@mcp.tool()
async def get_host_details(host_name: str) -> Dict[str, Any]:
    """
    Retrieves detailed information about a specific host from Zabbix.

    Args:
        host_name (str): The visible name of the host in Zabbix (e.g., "Webserver-Prod-01").

    Returns:
        Dict[str, Any]: A host object with complete details, including status, interfaces, proxy, groups, and templates.

    Raises:
        ValueError: If the host is not found.
    """
    zapi = _get_zabbix_connection()
    
    hosts = zapi.host.get(
        filter={"name": host_name},
        output="extend", # Required to get 'status' and other extended properties
        selectInterfaces=["interfaceid", "ip", "port", "type", "useip", "main", "available", "error", "errors_from", "disable_until"],
        selectParentProxies=["proxyid", "host"], # To get the monitoring proxy
        selectGroups=["groupid", "name"], # To get host groups
        selectTemplates=["templateid", "name"] # To get linked templates
    )
    
    if not hosts:
        raise ValueError(f"Host '{host_name}' not found.")
    
    # Since we filter by name, there should ideally be only one relevant result
    return hosts[0]


@mcp.tool()
async def get_problems(limit: int = 5, min_severity: int = 0, time_period_seconds: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Retrieves a list of current, unresolved problems (alerts) from Zabbix,
    and then fetches associated host details via trigger for each problem.

    Args:
        limit (int): The maximum number of problems to return. Defaults to 5. Maximum is 15.
        min_severity (int): The minimum problem severity (0-5: Info, Warning, Average, High, Disaster). Defaults to 0 (Info).
        time_period_seconds (Optional[int]): The duration in seconds to retrieve problems for (e.g., 3600 for last 1 hour).
                                             Maximum is 24 hours (86400 seconds).
                                             If None, retrieves recent problems without a specific time constraint.

    Returns:
        List[Dict[str, Any]]: A list of problem objects with detailed information,
                              including 'associated_host_name' and 'associated_host_id' fields.
    """
    if limit > 15:
        limit = 15
        print(f"Warning: Problem limit reduced to 15 as it exceeds the maximum allowed.")

    max_time_period = 24 * 3600 # 24 hours
    if time_period_seconds is not None and time_period_seconds > max_time_period:
        time_period_seconds = max_time_period
        print(f"Warning: Problem time period reduced to {max_time_period} seconds (24 hours) as it exceeds the maximum allowed.")

    zapi = _get_zabbix_connection()
    params: Dict[str, Any] = {
        "output": "extend",
        # Hapus "selectHosts" dari sini karena tampaknya tidak bekerja di lingkungan Anda
        "limit": limit,
        "sortfield": ["eventid"],
        "sortorder": "DESC",
        "min_severity": min_severity,
        "recent": True
    }

    if time_period_seconds is not None:
        params["timefrom"] = int(time.time()) - time_period_seconds

    problems = zapi.problem.get(**params)
    
    # --- BAGIAN BARU: Ambil Nama Host dari Trigger ---
    for problem in problems:
        problem['associated_host_name'] = "Unknown Host" # Default
        problem['associated_host_id'] = "N/A" # Default
        
        # Jika masalah berasal dari trigger (object = 0)
        if problem.get('object') == '0' and problem.get('objectid'):
            trigger_id = problem['objectid']
            try:
                # Panggil trigger.get untuk mendapatkan detail trigger, termasuk host
                triggers = zapi.trigger.get(
                    triggerids=trigger_id,
                    selectHosts=["hostid", "name"], # Minta detail host dari trigger
                    output="extend" # Untuk memastikan semua properti terkait ada
                )
                if triggers and triggers[0].get('hosts'):
                    # Ambil nama host dari trigger
                    host_name = triggers[0]['hosts'][0].get('name')
                    host_id = triggers[0]['hosts'][0].get('hostid')
                    if host_name:
                        problem['associated_host_name'] = host_name
                    if host_id:
                        problem['associated_host_id'] = host_id
            except Exception as e:
                print(f"Error fetching host for trigger {trigger_id}: {e}")
                problem['associated_host_name'] = f"Error fetching host: {e}"
        
        # Konversi clock ke format waktu yang lebih mudah dibaca (Opsional, bisa juga di LLM)
        # problem['formatted_time'] = datetime.datetime.fromtimestamp(int(problem['clock'])).strftime('%H:%M:%S')

    return problems

# --- Main Execution Block ---
if __name__ == "__main__":
    print(f"Starting ZabbixInfoServer MCP on http://localhost:3200")
    print("Ensure ZABBIX_SERVER_URL and ZABBIX_API_TOKEN are set in your .env file.")
    uvicorn.run(mcp.streamable_http_app, host="localhost", port=3200)