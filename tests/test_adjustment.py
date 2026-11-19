from fractions import Fraction
import json
from pathlib import Path
import unittest
from level_network.adjustment import adjust
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class AdjustmentTests(unittest.TestCase):
    def test_triangle_exact_solution_and_stationarity(self):
        r=adjust(sample())
        self.assertEqual(r['elevations'],{'A':'100','B':'304/3','C':'308/3'})
        self.assertEqual(r['weighted_squared_error'],'1/3')
        self.assertTrue(all(Fraction(v)==0 for v in r['normal_residual'].values()))
        self.assertEqual(r['degrees_of_freedom'],1)
    def test_consistent_network_has_zero_residual(self):
        data=sample();data['observations'][2]['delta']='2'
        r=adjust(data);self.assertEqual(r['weighted_squared_error'],'0')
        self.assertEqual(r['elevations']['C'],'102')
    def test_weighted_parallel_observations(self):
        data={'points':[{'id':'A','elevation':'0'},{'id':'B','elevation':None}],
              'observations':[{'id':'a','from':'A','to':'B','delta':'1','weight':'1'},
                              {'id':'b','from':'A','to':'B','delta':'3','weight':'3'}]}
        self.assertEqual(adjust(data)['elevations']['B'],'5/2')
    def test_orientation_reversal_preserves_heights(self):
        data=sample()
        for row in data['observations']:
            row['from'],row['to']=row['to'],row['from'];row['delta']=str(-int(row['delta']))
        self.assertEqual(adjust(data)['elevations'],adjust(sample())['elevations'])
    def test_disconnected_unknown_rejected(self):
        data=sample();data['points'].append({'id':'D','elevation':None})
        with self.assertRaisesRegex(ValueError,'datum'):adjust(data)
    def test_no_benchmark_rejected(self):
        data=sample();data['points'][0]['elevation']=None
        with self.assertRaisesRegex(ValueError,'benchmark'):adjust(data)
    def test_fixed_conflict_produces_residual(self):
        data={'points':[{'id':'A','elevation':'0'},{'id':'B','elevation':'2'}],
              'observations':[{'id':'ab','from':'A','to':'B','delta':'1','weight':'2'}]}
        self.assertEqual(adjust(data)['weighted_squared_error'],'2')
    def test_nonpositive_weight_rejected(self):
        for value in ('0','-1'):
            data=sample();data['observations'][0]['weight']=value
            with self.subTest(value=value),self.assertRaises(ValueError):adjust(data)
    def test_duplicate_observation_rejected(self):
        data=sample();data['observations'].append(data['observations'][0])
        with self.assertRaises(ValueError):adjust(data)
    def test_numeric_float_rejected(self):
        data=sample();data['observations'][0]['delta']=1.1
        with self.assertRaises(ValueError):adjust(data)
