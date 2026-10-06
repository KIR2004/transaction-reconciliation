CREATE TABLE IF NOT EXISTS transactions (
  transaction_id VARCHAR(50) PRIMARY KEY,
  lifecycle_id VARCHAR(50),
  account_id VARCHAR(50) NOT NULL,
  merchant_name VARCHAR(100) NOT NULL,
  transaction_date DATE NOT NULL,
  transaction_amount DECIMAL(10,2) NOT NULL,
  currency VARCHAR(3) NOT NULL,
  status VARCHAR(20) NOT NULL,
  settlement_status VARCHAR(20) DEFAULT 'PENDING',
  total_settled_amount DECIMAL(10,2) DEFAULT 0.00,
  last_settlement_date DATE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settlement_history (
  settlement_id VARCHAR(50) PRIMARY KEY,
  transaction_id VARCHAR(50),            -- NULL = unmatched settlement
  lifecycle_id VARCHAR(50),
  settlement_date DATE NOT NULL,
  settlement_amount DECIMAL(10,2) NOT NULL,
  settlement_type VARCHAR(10) NOT NULL,  -- 'DEBIT' or 'CREDIT'
  currency VARCHAR(3) NOT NULL,
  processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_txn_lifecycle ON transactions(lifecycle_id);
CREATE INDEX IF NOT EXISTS idx_txn_fallback ON transactions(account_id, merchant_name, transaction_date);
CREATE INDEX IF NOT EXISTS idx_set_txn ON settlement_history(transaction_id);
