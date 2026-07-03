import pandas as pd
import numpy as np
from datetime import datetime
import os
import traceback

class DataLoader:
    def __init__(self):
        self.sources = [
            'BUS-1', 'BUS-2', 'DELIVERY TRUCK', 
            'MOTORIZED VEHICLE', 'TOILET-LAVATORY', 
            'STREET FOODS', 'LINER-MARKET', 'TABO',
            'MARKET-RENTAL STALL-SPACE', 'MARKET ELECTRIC'
        ]
    
    def load_all_data(self):
        """Load data from Newcleandata.xlsx file"""
        try:
            # Read the Excel file
            df = pd.read_excel('Newcleandata.xlsx', sheet_name='Sheet1')
            
            # Mapping of column names to source names
            column_mapping = {
                'Stall Rental': 'MARKET-RENTAL STALL-SPACE',
                'Electric Bills': 'MARKET ELECTRIC',
                'Liner': 'LINER-MARKET',
                'Tabo': 'TABO',
                'Street Food': 'STREET FOODS',
                'Bus-S1': 'BUS-1',
                'Bus-S2': 'BUS-2',
                'Delivery Parking': 'DELIVERY TRUCK',
                'Motorized Vehicle': 'MOTORIZED VEHICLE',
                'Lavatory': 'TOILET-LAVATORY'
            }
            
            all_records = []
            
            # Process each row (month)
            for _, row in df.iterrows():
                date_str = row['Date']
                if pd.notna(date_str):
                    # Convert to datetime
                    try:
                        date = pd.to_datetime(date_str, format='%Y-%m')
                    except:
                        try:
                            date = pd.to_datetime(date_str)
                        except:
                            continue
                    
                    # Process each column/source
                    for col_name, source_name in column_mapping.items():
                        if col_name in row and pd.notna(row[col_name]) and row[col_name] > 0:
                            amount = float(row[col_name])
                            all_records.append({
                                'date': date,
                                'amount_remitted': amount,
                                'source': source_name,
                                'section': 'MARKET' if 'MARKET' in source_name else 'LAND AND TRANSPORT',
                                'year': date.year,
                                'month': date.month
                            })
            
            if all_records:
                result_df = pd.DataFrame(all_records)
                print(f"[OK] Loaded {len(result_df)} records from Newcleandata.xlsx")
                return self.clean_data(result_df)
            else:
                print("[WARN] No valid data found in Newcleandata.xlsx")
                return self.create_sample_data()
                
        except Exception as e:
            print(f"[ERROR] Error loading Newcleandata.xlsx: {e}")
            traceback.print_exc()
            return self.create_sample_data()
    
    def clean_data(self, df):
        """Clean and prepare data for modeling"""
        # Remove outliers
        mean = df['amount_remitted'].mean()
        std = df['amount_remitted'].std()
        df = df[df['amount_remitted'].between(mean - 3*std, mean + 3*std)]
        
        # Ensure dates are datetime
        df['date'] = pd.to_datetime(df['date'])
        
        # Sort by date
        df = df.sort_values('date')
        
        return df
    
    def create_sample_data(self):
        """Create sample data if no real data is available"""
        np.random.seed(42)
        data = []
        
        start_date = datetime(2022, 1, 1)
        for i in range(48):  # 4 years of monthly data
            date = start_date + pd.DateOffset(months=i)
            
            for source in self.sources:
                # Different base amounts for different sources
                if 'BUS' in source:
                    base = 50000
                elif 'TOILET' in source:
                    base = 30000
                elif 'STREET' in source:
                    base = 40000
                elif 'DELIVERY' in source:
                    base = 35000
                else:
                    base = 25000
                
                # Add seasonality and trend
                seasonal = 20000 * np.sin(2 * np.pi * i / 12)
                trend = 1000 * (i / 12)
                noise = np.random.normal(0, base * 0.1)
                
                amount = max(1000, base + seasonal + trend + noise)
                
                data.append({
                    'date': date,
                    'amount_remitted': amount,
                    'source': source,
                    'section': 'MARKET' if 'MARKET' in source else 'LAND AND TRANSPORT',
                    'year': date.year,
                    'month': date.month
                })
        
        return pd.DataFrame(data)