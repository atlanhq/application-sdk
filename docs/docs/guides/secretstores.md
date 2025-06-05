# Credential Management with Secret Stores

## Introduction

The Application SDK Framework provides built-in support for retrieving credentials from external secret stores. When building apps with this framework, developers can offer users the option to securely store and retrieve their database credentials from supported secret management services instead of entering them directly into the application.

Using secret stores offers several advantages:

- **Enhanced Security**: Credentials are stored and managed in a specialized secure service
- **Centralized Management**: Credentials can be rotated and updated in one place
- **Access Control**: Secret stores provide fine-grained access controls and audit logging
- **Compliance**: Help meet regulatory requirements for credential management

## Supported Secret Stores

The Application SDK Framework currently supports the following secret stores:

### AWS Secrets Manager

AWS Secrets Manager is a secure service that helps store, retrieve, and rotate database credentials, API keys, and other secrets throughout their lifecycle.

## Implementation Guide for App Developers

If you're developing an app using the Application SDK Framework, you can integrate secret store support by following these guidelines:

1. **Add credential source selection to your UI**:
   - Include a dropdown or similar control that allows users to select where their credentials are stored
   - Provide options for direct input and any supported secret stores (e.g., AWS Secrets Manager)

2. **Collect necessary metadata for the selected secret store**:
   - For AWS Secrets Manager: Secret ARN and AWS Region
   - Add appropriate form fields to collect this information

3. **Handle credential resolution**:
   - When processing credentials, use the framework's credential provider system
   - The framework will automatically resolve actual credentials based on the source selected

4. **Provide user guidance**:
   - Include information in your app about how to format and enter credential references

## Example UI Implementation

The code sample below shows how to implement a credential source dropdown in your app:

```html
<div class="form-group">
  <label>Where are your credentials stored? *</label>
  <select id="credentialSource" onchange="handleCredentialSourceChange()">
    <option value="direct">I will enter them below</option>
    <option value="aws-secrets">AWS Secrets Manager</option>
  </select>
</div>

<!-- AWS Secrets Manager Section (hidden by default) -->
<div id="aws-secrets-section" style="display: none;">
  <div class="form-group">
    <label>AWS Secret ARN *</label>
    <input type="text" id="aws-secret-arn" placeholder="arn:aws:secretsmanager:..." />
  </div>
  <div class="form-group">
    <label>AWS Region *</label>
    <input type="text" id="aws-secret-region" placeholder="us-east-1" />
  </div>
</div>
```

## End-User Guide to AWS Secrets Manager

### Setting Up AWS Secrets Manager

End users of your app can set up their credentials in AWS Secrets Manager by following these steps:

1. **Log in to the AWS Console**:
   - Navigate to the AWS Management Console
   - Open the AWS Secrets Manager service

2. **Create a New Secret**:
   - Click "Store a new secret"
   - Select "Other type of secret"
   - Enter credentials as key-value pairs:
     - For Basic authentication: Add keys for `username` and `password`
     - For IAM User authentication: Add keys for `username`, `access-key-id` and `secret-access-key`
     - For IAM Role authentication: Add keys for `username`, `role-arn` and `external-id`

3. **Configure Secret Settings**:
   - Give the secret a descriptive name
   - Configure rotation settings if desired

4. **Copy the Secret ARN**:
   - Once created, copy the ARN of the secret, which will look like: `arn:aws:secretsmanager:region:account-id:secret:secret-name-xxx`
   - Note the AWS region where the secret is stored

### Using Credentials from AWS Secrets Manager

When users set up a connection in your app, they should:

1. **Select AWS Secrets Manager** as the credential source
2. **Enter the Secret ARN and Region**
3. **Use key names as references**:
   - Instead of entering actual credentials, users should enter the key names from their secret
   - For example, if their secret contains a key named `postgres_password`, they would enter `postgres_password` in the Password field
4. The framework will automatically retrieve and use the actual values from AWS Secrets Manager

## Troubleshooting

Common issues users might encounter:

- **Connection Failures**:
  - Verify the Secret ARN is correct and accessible
  - Ensure the AWS region is correctly specified
  - Check that key names in the form exactly match the keys in the AWS secret
  - Verify that the platform/environment running the app has the appropriate IAM role or permissions to access the secret
  - Check AWS CloudTrail logs for any access denied errors

- **Invalid Credentials**:
  - Ensure the secret contains all required credential fields
  - Verify that credential values in the secret store are correct and up-to-date

## Technical Details for Framework Developers

The credential resolution process follows these steps:

1. The application collects credential information from the user interface
2. Based on the credential source, the appropriate credential provider is selected
3. For secret store providers, the necessary metadata (like ARN and region) is extracted
4. The provider connects to the secret store service and retrieves the actual credentials
5. Retrieved values are substituted for key references in the original credential object
6. The resolved credentials are used for the database connection

Future extensions to support additional secret stores should implement the `CredentialProvider` interface and register with the `CredentialProviderFactory`.