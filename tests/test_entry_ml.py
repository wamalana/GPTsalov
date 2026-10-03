import unittest
from gptsalov.entry_ml import split_rows, fit, probability, EMBARGO_MS

class MLTests(unittest.TestCase):
    def test_split_has_embargo_and_no_crossing_labels(self):
        rows=[dict(t=i*3600000,end=(i+4)*3600000,id=str(i)) for i in range(100)]
        train,val,test=split_rows(rows)
        self.assertTrue(train and val and test)
        self.assertLess(max(r['t'] for r in train),min(r['t'] for r in val)-EMBARGO_MS)
        self.assertLess(max(r['end'] for r in train),min(r['t'] for r in val))
        self.assertLess(max(r['end'] for r in val),min(r['t'] for r in test))
        self.assertFalse({r['id'] for r in train}&{r['id'] for r in test})

    def test_scaler_uses_only_training_data_and_learns_direction(self):
        rows=[dict(x=[float(i%2)]+[0.0]*9,y=i%2) for i in range(80)]
        model=fit(rows)
        self.assertEqual(model['mean'][0],.5)
        self.assertGreater(probability([1.0]+[0.0]*9,model),probability([0.0]*10,model))
        self.assertTrue(0 <= probability([1e10]*10,model) <= 1)

    def test_equal_times_never_split_across_groups(self):
        rows=[dict(t=i*3600000,end=(i+4)*3600000,id=f'{i}-{j}') for i in range(100) for j in range(3)]
        parts=split_rows(rows)
        for i,p in enumerate(parts):
            for other in parts[i+1:]:
                self.assertFalse({r['t'] for r in p}&{r['t'] for r in other})

if __name__=='__main__':unittest.main()
