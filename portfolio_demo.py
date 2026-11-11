"""Reproducible portfolio demo with synthetic data and a temporary database."""
from pathlib import Path
from tempfile import TemporaryDirectory
from service import Assistant
from maintenance import snapshot, verify_backup

def main():
    with TemporaryDirectory(prefix='portfolio-demo-') as folder:
        root = Path(folder)
        api = Assistant(root / 'demo.sqlite3')
        purchase = api.record_purchase('demo-buy', name='演示雨伞', quantity=2,
                                       unit='把', received=True)
        def show(label):
            quantity = sum(i['quantity'] for i in api.inventory())
            amount = api.spending()['by_currency'].get('CNY', {}).get('net_minor')
            print(f'{label}: quantity={quantity}, net_minor={amount}')
        show('1. Purchase without amount')
        retried = api.record_purchase('demo-buy', name='演示雨伞', quantity=2,
                                     unit='把', received=True)
        assert retried == purchase
        assert sum(i['quantity'] for i in api.inventory()) == 2
        show('2. Retry same request: no duplicate inventory')
        api.correct_purchase_amount('demo-price', purchase['purchase_id'],
                                    expected_version=1, total_amount_minor=6000, currency='CNY')
        show('3. Correct purchase amount: inventory unchanged')
        item = api.inventory()[0]
        returned = api.return_item('demo-return', item['id'], quantity=1,
                                   expected_version=item['version'], infer_amount=True)
        show('4. Return one: deduct expense immediately')
        api.undo('demo-undo', returned['operation_id'])
        show('5. Undo return')
        result = snapshot(api.path, root / 'backups')
        verify_backup(result['backup'])
        print('6. Consistent backup: checksum and database validation passed')
    print('Temporary synthetic data removed.')

if __name__ == '__main__':
    main()
