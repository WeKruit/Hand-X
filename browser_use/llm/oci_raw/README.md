# OCI Raw API Integration

This module provides direct integration with Oracle Cloud Infrastructure's Generative AI service using raw API calls, without Langchain dependencies.

## Features

- **Direct API Integration**: Uses OCI's native Python SDK for direct API calls
- **Async Support**: Full async/await support for non-blocking operations
- **Structured Output**: Support for Pydantic model validation of responses
- **Error Handling**: Comprehensive error handling with proper exception types
- **Authentication**: Support for multiple OCI authentication methods

## Installation

Make sure you have the required OCI dependencies installed:

```bash
pip install oci
```

## Usage

### Basic Usage

```python
from browser_use import Agent
from browser_use.llm import ChatOCIRaw

# Configure the model
model = ChatOCIRaw(
    model_id="ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceya...",
    service_endpoint="https://inference.generativeai.us-chicago-1.oci.oraclecloud.com",
    compartment_id="ocid1.tenancy.oc1..aaaaaaaayeiis5uk2nuubznrekd...",
    provider="meta",  # or "cohere"
    temperature=1.0,
    max_tokens=600,
    top_p=0.75,
    auth_type="API_KEY",
    auth_profile="DEFAULT"
)

# Use with browser-use Agent
agent = Agent(
    task="Search for Python tutorials and summarize them",
    llm=model
)

history = await agent.run()
```

### Structured Output

```python
from pydantic import BaseModel

class SearchResult(BaseModel):
    title: str
    summary: str
    relevance_score: float

# Use structured output
response = await model.ainvoke(messages, output_format=SearchResult)
result = response.completion  # This is a SearchResult instance
```

## Configuration

### Authentication Types

The integration supports multiple OCI authentication methods:

- `API_KEY`: Uses API key authentication (default)
- `INSTANCE_PRINCIPAL`: Uses instance principal authentication
- `RESOURCE_PRINCIPAL`: Uses resource principal authentication

### Model Parameters

- `model_id`: The OCID of your OCI GenAI model
- `service_endpoint`: The OCI service endpoint URL
- `compartment_id`: The OCID of your OCI compartment
- `provider`: Model provider ("meta" or "cohere")
- `temperature`: Response randomness (0.0-2.0)
- `max_tokens`: Maximum tokens in response
- `top_p`: Top-p sampling parameter
- `frequency_penalty`: Frequency penalty for repetition
- `presence_penalty`: Presence penalty for repetition

## Error Handling

The integration provides proper error handling with specific exception types:

- `ModelRateLimitError`: For rate limiting (429 errors)
- `ModelProviderError`: For other API errors (4xx, 5xx)

## Comparison with Langchain Integration

| Feature | OCI Raw API | Langchain Integration |
|---------|-------------|----------------------|
| Dependencies | OCI SDK only | Langchain + OCI SDK |
| Performance | Direct API calls | Additional abstraction layer |
| Control | Full control over requests | Limited by Langchain interface |
| Updates | Direct OCI SDK updates | Dependent on Langchain updates |
| Complexity | Lower complexity | Higher complexity |

## Example Response Format

The OCI GenAI API returns responses in this format:

```json
{
  "chat_response": {
    "api_format": "GENERIC",
    "choices": [
      {
        "finish_reason": "stop",
        "index": 0,
        "message": {
          "content": [
            {
              "text": "Response text here",
              "type": "TEXT"
            }
          ],
          "role": "ASSISTANT"
        }
      }
    ],
    "usage": {
      "completion_tokens": 18,
      "prompt_tokens": 38,
      "total_tokens": 56
    }
  }
}
```

## Troubleshooting

### Common Issues

1. **Authentication Errors**: Ensure your OCI configuration is correct and you have the necessary permissions
2. **Model Not Found**: Verify your model OCID and ensure it's available in your compartment
3. **Rate Limiting**: The integration handles rate limits automatically with proper error types

### Debug Mode

Enable verbose logging by setting the `verbose` parameter to `True` (not implemented in this version but can be added).

## Contributing

When contributing to this module:

1. Follow the existing code style
2. Add proper type hints
3. Include comprehensive error handling
4. Add tests for new features
5. Update documentation
