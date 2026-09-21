# IBM COS Hive Management Script

## Overview
This script provides modular functions to manage IBM Cloud Object Storage (COS) Hive buckets, storage accounts, and credentials. It is designed to streamline the setup and management process for Hive buckets across different environments (dev, tst, qa, prd).

## Features
- **Storage Accounts Creation**: Automatically creates storage accounts for each environment.
- **Buckets Creation**: Sets up buckets with predefined quotas and firewall rules for each environment.
- **Credentials Generation**: Generates and exports credentials for accessing the buckets.
- **Credentials Retrieval**: Retrieves existing credentials for buckets.
- **Bucket Listing**: Lists every bucket across all Hive storage accounts (`sa-ctie-hive-*`), driven by the API's own account list rather than a guessed sequence.

## Prerequisites
- Python 3.x
- `requests` library installed (`pip install requests`)
- Access to the IBM COS API with appropriate permissions
- Valid authorization token for the IBM COS API
- SSL Certificate (if the API uses a self-signed or custom certificate)

## Configuration
The script uses a `config.json` file to store all configurations. Modify this file to define:

1. **Buckets**: List of buckets with their storage accounts, names, and quotas for each environment.
2. **Environments**: List of environments (e.g., dev, tst, qa, prd).
3. **API Settings**: Base URL and headers for the IBM COS API.
4. **Storage Locations**: Storage locations for each environment.
5. **Firewall Rules**: Allowed IPs for each environment.
6. **SSL Settings**: Configure SSL verification and optionally specify a custom CA bundle path if the API uses a self-signed or custom certificate.

### SSL Configuration
If the IBM COS API uses a self-signed or custom certificate, you need to:
1. **Disable SSL Verification (Not Recommended for Production)**:
   ```json
   "ssl_verify": false
   ```

2. **Use a Custom CA Bundle (Recommended)**:
   - Obtain the CA certificate and save it to a file (e.g., `osiris-ca.pem`).
   - Update the `config.json` file to specify the path to the CA bundle:
     ```json
     "ssl_verify": true,
     "ca_bundle_path": "/path/to/osiris-ca.pem"
     ```

## Usage
1. Clone the repository or download the script.
2. Install the required dependencies:
   ```bash
   pip install requests
   ```
3. Update the `config.json` file with your settings.
4. Run the script with the desired action:
   - To create storage accounts, buckets, and credentials:
     ```bash
     python hive_management.py --action create_all
     ```
   - To retrieve existing credentials:
     ```bash
     python hive_management.py --action get_creds
     ```
   - To list every bucket across all Hive storage accounts:
     ```bash
     python hive_management.py --action list_buckets
     ```
     By default this lists accounts whose id starts with `sa-ctie-hive-`
     (override with `--account-prefix`), and prints each account's
     buckets to stdout. Add `--output buckets.json` to also write the
     result as `{"account-id": ["bucket1", "bucket2", ...]}`.

## Output
- **Credentials Files**: The script generates credential files for each bucket in the format `ctie-hive-<bucket_name>` or `ctie-hive-<bucket_name>.json`. Each file contains the access key ID and secret key for the respective environments.
- **Bucket Listing**: With `--action list_buckets`, a summary is printed for each Hive account, and optionally written as JSON via `--output`.

## Notes
- Ensure that the IBM COS API endpoint and ports are accessible from your environment.
- Verify that the firewall rules are correctly configured to allow access from the intended IP ranges.
- The script assumes the existence of specific storage locations (`cv-ctie-hive-dev-01`, `cv-ctie-hive-tst-01`, etc.). Adjust these as needed in the `config.json` file.
- `list_buckets` calls the IBM COS Container Mode Service API's container-listing operation (`GET <accesser>/accounts/{account-id}/containers`), which the API guide flags as resource-intensive on the system. Use it on demand for audits, not in a tight loop or a scheduled/automated job.

## License
This script is provided as-is. Ensure compliance with your organization's policies and IBM COS terms of service.

## Extending the Script
To extend the script for additional functionality:
1. Add new functions to the `hive_management.py` script.
2. Update the `config.json` file with any new configurations.
3. Add new actions to the `argparse` section in the `main` function.
