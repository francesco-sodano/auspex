param keyVaultName string

@secure()
param priceApiKey string = ''

@secure()
param newsApiKey string = ''

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource priceSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(priceApiKey)) {
  parent: keyVault
  name: 'ALPHAVANTAGE-API-KEY'
  properties: {
    value: priceApiKey
  }
}

resource newsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(newsApiKey)) {
  parent: keyVault
  name: 'FINNHUB-API-KEY'
  properties: {
    value: newsApiKey
  }
}
