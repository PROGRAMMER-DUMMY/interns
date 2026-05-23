import duckdb

from core.presentation.console_tables import render_query_result_table

conn = duckdb.connect(":memory:")

# Load the base data
conn.execute("CREATE OR REPLACE VIEW encounters AS SELECT * FROM read_csv_auto('workspaces/Hospital_Patient_Records/encounters.csv')")
conn.execute("CREATE OR REPLACE VIEW procedures AS SELECT * FROM read_csv_auto('workspaces/Hospital_Patient_Records/procedures.csv')")

print("### HOSPITAL ANALYTICS RESULTS ###")

print("\n1. Total Encounters by Year (Top 5)")
print(render_query_result_table(conn.execute("""
    SELECT strftime(CAST(START AS TIMESTAMP), '%Y') as Year, count(*) as Total_Encounters 
    FROM encounters 
    GROUP BY 1 ORDER BY 1 DESC LIMIT 5
""")))

print("\n2. Encounters by Class (Top 5)")
print(render_query_result_table(conn.execute("""
    SELECT ENCOUNTERCLASS, count(*) as Count, round(count(*) * 100.0 / (SELECT count(*) FROM encounters), 2) as Percentage
    FROM encounters 
    GROUP BY 1 ORDER BY 2 DESC LIMIT 5
""")))

print("\n3. Procedures by Frequency (Top 5)")
print(render_query_result_table(conn.execute("""
    SELECT DESCRIPTION, count(*) as Count, round(avg(BASE_COST), 2) as Avg_Base_Cost
    FROM procedures 
    GROUP BY 1 ORDER BY 2 DESC LIMIT 5
""")))

print("\n4. Payer Cost Analysis (Top 5)")
print(render_query_result_table(conn.execute("""
    SELECT e.PAYER, count(e.Id) as Encounter_Count, round(avg(e.TOTAL_CLAIM_COST), 2) as Avg_Total_Cost
    FROM encounters e
    GROUP BY 1 ORDER BY 3 DESC LIMIT 5
""")))

conn.close()
