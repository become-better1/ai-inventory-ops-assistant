"""Safe local demonstration: only creates a temporary database."""
from pathlib import Path
from tempfile import TemporaryDirectory
from service import Assistant

def main():
    with TemporaryDirectory() as folder:
        api=Assistant(Path(folder)/'demo.sqlite3')
        purchase=api.record_purchase('buy',name='G304',quantity=2,
            total_amount_minor=40000,currency='CNY',received=True)
        def show(label):
            qty=sum(i['quantity'] for i in api.inventory())
            net=api.spending()['by_currency']['CNY']['net_minor']
            print(f'{label}: inventory={qty}, net=CNY {net/100:.2f}')
        show('Purchased')
        returned=api.return_item('return',purchase['item_id'],quantity=1,
            expected_version=1,infer_amount=True)
        show('Returned one')
        api.undo('undo',returned['operation_id'])
        show('Undid return')
    print('Temporary demonstration data removed; personal database untouched.')

if __name__=='__main__':
    main()

