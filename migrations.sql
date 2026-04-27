-- Migration: Add temporary member support to ledger_members
-- Run this to add support for temporary (non-registered) members

ALTER TABLE ledger_members 
ADD COLUMN IF NOT EXISTS is_temporary BOOLEAN DEFAULT FALSE;

ALTER TABLE ledger_members 
ADD COLUMN IF NOT EXISTS temporary_name VARCHAR(100);

-- Make user_id nullable for temporary members
ALTER TABLE ledger_members 
ALTER COLUMN user_id DROP NOT NULL;

-- Add unique constraint for temporary members (ledger_id + temporary_name)
-- Note: This is optional, depends on your business logic
