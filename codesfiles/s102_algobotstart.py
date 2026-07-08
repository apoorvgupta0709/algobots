# SAFETY GUARD: this legacy bot logs into FYERS at import time and places
# REAL orders — including SHORT option legs — with no gate, dry-run mode, or
# risk caps. It is kept for reference only and refuses to run without an
# explicit acknowledgement.
import os as _os

if _os.getenv("ALGOBOT_LEGACY_LIVE_ACK") != "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS":
    raise SystemExit(
        "Refusing to run: legacy live bot disabled. It places real FYERS "
        "orders (including short options) with no safety gate. Set "
        "ALGOBOT_LEGACY_LIVE_ACK=I_UNDERSTAND_THIS_PLACES_REAL_ORDERS "
        "only if you truly intend live trading.")

from datetime import datetime, timedelta, time
import pandas as pd
import pytz
from s4_add_indicator import bnf_data
import time as tm
import os

from s7_1fyerslogin import fyerslogin_class
a = fyerslogin_class()
fyers = a.auto_login()

# Order Position
def orderPosition():
    response = fyers.positions()
    return response


# Fetch Data
def hist_data(symbol):
    tdate = datetime.now().date() 
    fdate = tdate - timedelta(days=5)
    data = {
        "symbol":symbol,
        "resolution":"5",
        "date_format":"1", # 0 -> for epoch, 1 -> for yyyy-mm-dd
        "range_from":fdate,
        "range_to":tdate,
        "cont_flag":"1" # 1 for continous data for futures and options
    }

    response = fyers.history(data=data)
    #print(response)

    bnf = pd.DataFrame(response['candles'], columns = ['Date', 'open', 'high', 'low', 'close', 'volume'])

    bnf['Date'] = bnf['Date'].apply(pd.Timestamp, unit = 's', tzinfo = pytz.timezone('Asia/Kolkata'))
    bnf = bnf.sort_values(by = 'Date')


    #Assuming 'Date' is the name of your date column and bnf is your DataFradf['Date'] = pd.to_datetime(bnf['Date'])
    bnf.set_index = bnf['Date'].dt.strftime('%d-%m-%Y')
    bnf['Time'] = bnf['Date'].dt.strftime('%H:%M')
    bnf['Date_w'] = bnf['Date'].dt.strftime('%d-%m-%Y')


    # Drop the original 'Date' column if needed
    #bnf = bnf.drop(columns=['Date'])

    return bnf


#Update Data
def tableUpdate(filepath):
    df = hist_data('NSE:NIFTYBANK-INDEX')
    df.to_csv(filepath)
    x = bnf_data(e = 14, r = 25, bbl = 5, bbs = 0.8, eb = 5, lr = 70, sr = 70, a = 7,sl_ratio = 0.5, rr = 1.8, filepath = filepath)
    x.full_loop()
    return x.df_bnfclass

#Trade Log
def tradeLog(entry):
    file_path = 'Trade Log/Trade Details.csv'
    tpsl_file = pd.read_csv(file_path)
    tpsl_file.set_index(tpsl_file.columns[0], inplace=True)
    temp_df = pd.DataFrame([entry])

    # Check if file exists and is not empty
    # if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
    #     # File exists and is not empty, append the data
    #     temp_df.to_csv(file_path, mode='a', header=False, index=False)
    # else:
    #     # File does not exist or is empty, write the data
    #     temp_df.to_csv(file_path, mode='w', header=True, index=False)
    tpsl_file = pd.concat([tpsl_file, temp_df], ignore_index=True)
    #tpsl_file.set_index('orderID', inplace = True)
    #tpsl_file = tpsl_file.iloc[:]
    tpsl_file.to_csv('Trade Log/Trade Details.csv')   

# Index Price
def bnf_index():
    data = {"symbols" : "NSE:NIFTYBANK-INDEX"}
    bnf = fyers.quotes(data)
    bnf = round(bnf['d'][0]['v']['lp'])
    return bnf

# Place Order
def placeOrder(expiry, strike, orderTag, optionType, limit = 0, stop = 0, qty = 15, side = 1,
                type = 2, productType = 'INTRADAY', validity = 'DAY'):
    
    data = {
        "symbol":f'NSE:BANKNIFTY{expiry}{strike}{optionType}',
        "qty":qty,
        "type":type, # 1 -> Limit Order, 2 -> Market Order, 3 -> Stop Order, 4 -> Stop Limit Order
        "side":side, # 1 -> Buy, -1 -> Sell
        "productType":productType, # CNC, INTRADAY, MARGIN, CO, BO
        "limitPrice":limit, # Limit and Stoplimit Order
        "stopPrice":stop, # # Stop and Stoplimit Order
        "validity":validity, # IOC or DAY
        "disclosedQty":0, # stopLoss -> for CO and BO orders, takeProfit -> for BO orders
        "offlineOrder":False, # FALSE -> Market Open, TRUE -> AMO
        "orderTag":orderTag
    }
    
    response = fyers.place_order(data=data)
    print(response)

    return response

# Place Order
def placeOrder1(orderTag, limit = 0, stop = 0, qty = 15, side = 1, type = 2, productType = 'CNC', validity = 'DAY', symbol = 'NSE:SBIN-EQ'):
    response = orderPosition()
    if response['netPositions'] == []:
        data = {
            "symbol":symbol,
            "qty":qty,
            "type":type, # 1 -> Limit Order, 2 -> Market Order, 3 -> Stop Order, 4 -> Stop Limit Order
            "side":side, # 1 -> Buy, -1 -> Sell
            "productType":productType, # CNC, INTRADAY, MARGIN, CO, BO
            "limitPrice":limit, # Limit and Stoplimit Order
            "stopPrice":stop, # # Stop and Stoplimit Order
            "validity":validity, # IOC or DAY
            "disclosedQty":0, # stopLoss -> for CO and BO orders, takeProfit -> for BO orders
            "offlineOrder":False, # FALSE -> Market Open, TRUE -> AMO
            "orderTag":orderTag
        }
        response = fyers.place_order(data=data)
        print(response)

    else :
        a = response['netPositions']['symbol']
        print(f'print position already open for {a}')


def main():    
    # Get the current datetime
    now = datetime.now()

    # Create a datetime object for today with the specific time (9:15:01)
    specific_time = datetime(now.year, now.month, now.day, 9, 20, 11)

    # Calculate the difference
    difference = specific_time - now

    # Get the total number of seconds
    seconds = difference.total_seconds()

    seconds

    freq = 300



    # Logic for buying -> Buy CE if Total Signal is 2 and PE if it is 1
    filepath = './Trade Log/file for trading bot.csv'
    expiry = '25JUL'
    slatr = 1.4
    rrr = 1.9

    if datetime.now().time() < time(9,20,11) :
        print(f'sleeping for {seconds} seconds')
        tm.sleep(seconds)
        
        
    while ((time(9, 20, 11) < datetime.now().time() < time(23, 30, 1))):
        response = fyers.positions()
        #print(response)
        print('************************************')
        bnf = tableUpdate(filepath = filepath)
        print(datetime.now().date())
        print(datetime.now().time())
        bc = -2
        print(f'close : {bnf.close[bc]}, rsi : {bnf.RSI[bc]}, bbh : {bnf.bbh[bc]}, bbl : {bnf.bbl[bc]}, atr : {bnf.atr[bc]}')
        print(f'ema20signal : {bnf.ema20signal[bc]}, TotalSignal : {bnf.TotalSignal[bc]}, TotalSignal1 : {bnf.TotalSignal1[bc]}' )
        if ((response['overall']['count_open'] == 0) or (response['netPositions'] == [])) :
                
                
                print('Inside if loop for null open positions')
                #bnf.TotalSignal[-1] = 1
                if bnf['TotalSignal1'][bc] == 1:
                    print("short call")
                    strike = int(bnf.close[bc]/100)*100
                    print(f'Strike selected is : {strike}')
                    placeOrder(expiry = expiry, strike = strike, orderTag = 'short',
                            optionType = 'PE')
                    sl1 = bnf.close[bc] + slatr * bnf.atr[bc]
                    tp1 = bnf.close[bc] - slatr * rrr * bnf.atr[bc]
                    entry = {'time' : datetime.now().time(), 'orderID' : 2, 'bnf_price' : int(bnf.close[bc]),
                            'cepe' : 'PE', 'slValues' : int(sl1), 'tpValues' : int(tp1),
                            'strike' : strike
                            }
                    tradeLog(entry)
                    print('Trade Log Entered')
                    tm.sleep(freq-290)
                

                elif bnf['TotalSignal1'][bc] == 2:
                    print("go long")
                    strike = int(bnf.close[bc]/100)*100
                    print(f'Strike selected is : {strike}')
                    placeOrder(expiry = expiry, strike = strike, orderTag = 'long',
                                optionType = 'CE')
                    
                    sl1 = bnf.close[bc] - slatr * bnf.atr[bc]
                    tp1 = bnf.close[bc] + slatr * rrr * bnf.atr[bc]
                    entry = {'time' : datetime.now().time(), 'orderID' : 3, 'bnf_price' : int(bnf.close[bc]),
                            'cepe' : 'CE', 'slValues' : int(sl1), 'tpValues' : int(tp1),
                            'strike' : strike
                            }
                    tradeLog(entry)
                    print('Trade Log Entered')
                    tm.sleep(freq-290)

                else :
                    print("waiting for signal")
                    tm.sleep(freq-270)

                #print(datetime.now().time(), time(19,54,0))
                if datetime.now().time() > time(15, 30, 1):
                    runCondition = False
                    print("Day End")
            

            


        else :
            # monitor Index price for SL and TP
            print('In Monitoring Part', response['overall'])
            bnfIndex = bnf_index()
            #tpsl_file = pd.read_csv('Trade Log/Trade Details.csv', delimiter='\t')
            tpsl_file = pd.read_csv('Trade Log/Trade Details.csv')

            # orderID = tpsl_file.index[-1]
            # cepe = str(tpsl_file.loc[orderID, 'cepe'])
            # bnf_price = int(tpsl_file.loc[orderID, 'bnf_price'])
            # sl = int(tpsl_file.loc[orderID, 'slValues'])
            # tp = int(tpsl_file.loc[orderID, 'tpValues'])
            # strike = int(tpsl_file.loc[orderID, 'strike'])

            orderID = tpsl_file.index[-1]
            cepe = str(tpsl_file['cepe'].iloc[-1])
            bnf_price = int(tpsl_file['bnf_price'].iloc[-1])
            sl = int(tpsl_file['slValues'].iloc[-1])
            tp = int(tpsl_file['tpValues'].iloc[-1])
            strike = int(tpsl_file['strike'].iloc[-1])



            # cepe = 'PE'
            # bnf_price = 47699
            # sl = 47850
            # tp = 47700

            print(f'cepe : {cepe}, bnf : {bnf_price}, sl : {sl}, tp : {tp}, strike : {strike}, ltp : {bnfIndex}')

            # read positions from fyers
            response = fyers.positions()
            for m in response['netPositions']:
                if m['netQty'] > 0 :
                    cepe1 = m['symbol'][-2:]
                    expiry1 = m['symbol'][-12:-7]
                    strike1 = int(m['symbol'][-7:-2])

                    # compare cepe, strike and expiry mismatch in csv read and fyers positions
                    if ((cepe != cepe1) or (strike != strike1) or (expiry != expiry1)):
                        print("Mismatch in sl tp values in excel and open position. Please rectify manually")
                        print(f'cepe1 : {cepe1}, strike1 : {strike1}, expiry1 : {expiry1}')
                        print(f'cepe : {cepe}, strike : {strike}, expiry : {expiry}')
                        print(f'{cepe != cepe1}, {strike != strike1}, {expiry != expiry1}')
                        break
            
                    if ((cepe1 == 'CE') & (bnfIndex < sl)):

                        response = placeOrder(expiry = expiry1, strike = strike1, orderTag = 'CEsl',
                                        optionType = cepe1, side = -1)
                        
                        print('cepe is CE and bnf index < sl')
                        tm.sleep(freq)
                        
                        
                    elif ((cepe1 == 'CE') & (bnfIndex > tp)):
                        response = placeOrder(expiry = expiry1, strike = strike1, orderTag = 'CEtp',
                                        optionType = cepe1, side = -1)
                        
                        print('cepe is CE and bnf index > tp')
                        tm.sleep(freq)
                        
                    elif (cepe1 == 'PE') & (bnfIndex > sl):
                        response = placeOrder(expiry = expiry1, strike = strike1, orderTag = 'PEsl',
                                        optionType = cepe1, side = -1)
                        
                        print('cepe is PE and bnf index > sl')
                        tm.sleep(freq)
                        
                    elif (cepe1 == 'PE') & (bnfIndex < tp):
                        response = placeOrder(expiry = expiry1, strike = strike1, orderTag = 'PEtp',
                                        optionType = cepe1, side = -1)
                        
                        print('cepe is CE and bnf index < tp')
                        tm.sleep(freq)
                        
                    else :
                        print("Don't know what to do !!!")

            tm.sleep(freq-290)

if __name__ == "__main__":
    main()
 