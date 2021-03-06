from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, HANDSHAKE_DISPATCHER
from ryu.controller.handler import set_ev_cls
import ryu.ofproto.ofproto_v1_3 as ofproto
import ryu.ofproto.ofproto_v1_3_parser as ofparser
import ryu.ofproto.openstate_v1_0 as osproto
import ryu.ofproto.openstate_v1_0_parser as osparser
from ryu.lib.packet import packet
from ryu.topology import event
import logging
from sets import Set
import time
import os

import sys
sys.path.append(os.path.abspath("/home/mininet/spider/src"))
import SPIDER_parser as f_t_parser
from ryu.lib import hub
import subprocess

realiz_num = os.environ['realiz_num']
delta_6 = os.environ['delta_6']
TRAFFIC_RATE = eval(os.environ['TRAFFIC_RATE'])
PEAK_RATE = os.environ['PEAK_RATE']
STEP = os.environ['STEP']
    
class OpenStateFaultTolerance(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(OpenStateFaultTolerance, self).__init__(*args, **kwargs)

        f_t_parser.detection_timeouts_list=[(eval(delta_6),0.1,10)]

        results_hash = f_t_parser.md5sum_results()
        if f_t_parser.network_has_changed(results_hash):
            f_t_parser.erase_figs_folder()

        (self.requests,self.faults) = f_t_parser.parse_ampl_results_if_not_cached()

        print len(self.requests), 'requests loaded'
        print len(self.faults), 'faults loaded'

        print "Building network graph from network.xml..."
        # G is a NetworkX Graph object
        (self.G, self.pos, self.hosts, self.switches, self.mapping) = f_t_parser.parse_network_xml()
        print 'Network has', len(self.switches), 'switches,', self.G.number_of_edges()-len(self.hosts), 'links and', len(self.hosts), 'hosts'

        print "NetworkX to Mininet topology conversion..."
        # mn_topo is a Mininet Topo object
        self.mn_topo = f_t_parser.networkx_to_mininet_topo(self.G, self.hosts, self.switches, self.mapping)
        # mn_net is a Mininet object
        self.mn_net = f_t_parser.create_mininet_net(self.mn_topo)

        f_t_parser.launch_mininet(self.mn_net)

        self.ports_dict = f_t_parser.adapt_mn_topo_ports_to_old_API(self.mn_topo.ports)

        f_t_parser.mn_setup_MAC_and_IP(self.mn_net)

        f_t_parser.mn_setup_static_ARP_entries(self.mn_net)

        f_t_parser.draw_network_topology(self.G,self.pos,self.ports_dict,self.hosts)

        (self.fault_ID, self.flow_entries_dict, self.flow_entries_with_timeout_dict, self.flow_entries_with_burst_dict) = f_t_parser.generate_flow_entries_dict(self.requests,self.faults,self.ports_dict,match_flow=f_t_parser.get_mac_match_mininet,check_cache=False,dpctl_script=False)

        # Associates dp_id to datapath object
        self.dp_dictionary=dict()
        # Associates dp_id to a dict associating port<->MAC address
        self.ports_mac_dict=dict()

        # Needed by fault_tolerance_rest --> servira' ancora se memorizzo tutte le variabili qui??
        self.f_t_parser = f_t_parser

        # switch counter
        self.switch_count = 0

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath

        self.ports_mac_dict[datapath.id] = dict()
        self.send_features_request(datapath)
        self.send_port_desc_stats_request(datapath)

        self.configure_stateful_stages(datapath)
        self.install_flows(datapath)
        
        self.dp_dictionary[datapath.id] = datapath

    def install_flows(self,datapath):
        print("Configuring flow table for switch %d" % datapath.id)

        if datapath.id in self.flow_entries_dict.keys():
            for table_id in self.flow_entries_dict[datapath.id]:
                for match in self.flow_entries_dict[datapath.id][table_id]:
                    mod = ofparser.OFPFlowMod(
                        datapath=datapath, cookie=0, cookie_mask=0, table_id=table_id,
                        command=ofproto.OFPFC_ADD, idle_timeout=0, hard_timeout=0,
                        priority=self.flow_entries_dict[datapath.id][table_id][match]['priority'], buffer_id=ofproto.OFP_NO_BUFFER,
                        out_port=ofproto.OFPP_ANY,
                        out_group=ofproto.OFPG_ANY,
                        flags=0, match=match, instructions=self.flow_entries_dict[datapath.id][table_id][match]['inst'])
                    datapath.send_msg(mod)

        self.switch_count += 1
        if self.switch_count == self.G.number_of_nodes():
            self.monitor_thread = hub.spawn(self._monitor,datapath) 

    def send_features_request(self, datapath):
        req = ofparser.OFPFeaturesRequest(datapath)
        datapath.send_msg(req)

    def configure_stateful_stages(self, datapath):
        node_dict = f_t_parser.create_node_dict(self.ports_dict,self.requests)

        self.send_table_mod(datapath, table_id=2)
        self.send_key_lookup(datapath, table_id=2, fields=[ofproto.OXM_OF_ETH_SRC,ofproto.OXM_OF_ETH_DST])
        self.send_key_update(datapath, table_id=2, fields=[ofproto.OXM_OF_ETH_SRC,ofproto.OXM_OF_ETH_DST])

        self.send_table_mod(datapath, table_id=3)
        self.send_key_lookup(datapath, table_id=3, fields=[ofproto.OXM_OF_METADATA])
        self.send_key_update(datapath, table_id=3, fields=[ofproto.OXM_OF_METADATA])

    def configure_global_states(self, datapath):
        for port in self.ports_mac_dict[datapath.id]:
            if port!=ofproto.OFPP_LOCAL:
                (global_state, global_state_mask) = osparser.masked_global_state_from_str("1",port-1)
                msg = osparser.OFPExpSetGlobalState(datapath=datapath, global_state=global_state, global_state_mask=global_state_mask)
                datapath.send_msg(msg)

    def send_table_mod(self, datapath, table_id, stateful=1):
        req = osparser.OFPExpMsgConfigureStatefulTable(datapath=datapath, table_id=table_id, stateful=stateful)
        datapath.send_msg(req)

    def send_key_lookup(self, datapath, table_id, fields):
        key_lookup_extractor = osparser.OFPExpMsgKeyExtract(datapath=datapath, command=osproto.OFPSC_EXP_SET_L_EXTRACTOR, fields=fields, table_id=table_id)
        datapath.send_msg(key_lookup_extractor)

    def send_key_update(self, datapath, table_id, fields):
        key_update_extractor = osparser.OFPExpMsgKeyExtract(datapath=datapath, command=osproto.OFPSC_EXP_SET_U_EXTRACTOR, fields=fields, table_id=table_id)
        datapath.send_msg(key_update_extractor)

    def set_link_down(self,node1,node2):
        if(node1 > node2):
            node1,node2 = node2,node1

        os.system('sudo ifconfig s'+str(node1)+'-eth'+str(self.ports_dict['s'+str(node1)]['s'+str(node2)])+' down')
        os.system('sudo ifconfig s'+str(node2)+'-eth'+str(self.ports_dict['s'+str(node2)]['s'+str(node1)])+' down')

    def set_link_up(self,node1,node2):
        if(node1 > node2):
            node1,node2 = node2,node1

        os.system('sudo ifconfig s'+str(node1)+'-eth'+str(self.ports_dict['s'+str(node1)]['s'+str(node2)])+' up')
        os.system('sudo ifconfig s'+str(node2)+'-eth'+str(self.ports_dict['s'+str(node2)]['s'+str(node1)])+' up')
            
    def send_port_desc_stats_request(self, datapath):
        req = ofparser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        # store the association port<->MAC address
        for p in ev.msg.body:
            self.ports_mac_dict[ev.msg.datapath.id][p.port_no]=p.hw_addr

        self.configure_global_states(ev.msg.datapath)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath

        pkt = packet.Packet(msg.data)
        header_list = dict((p.protocol_name, p) for p in pkt.protocols if type(p) != str)
        
        #discard IPv6 multicast packets
        if not header_list['ethernet'].dst.startswith('33:33:'):
            print("\nSecond fault detected: packet received by the CTRL")
            print(pkt)

    @set_ev_cls(ofp_event.EventOFPExperimenterStatsReply, MAIN_DISPATCHER)
    def state_stats_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath

        if ev.msg.body.exp_type==0:
            # EXP_STATE_STATS
            stats = osparser.OFPStateStats.parser(ev.msg.body.data, offset=0)
            for stat in stats:
                if stat.entry.key != []:
                    msg = osparser.OFPExpMsgSetFlowState(
                        datapath=dp, state=0, keys=stat.entry.key, table_id=stat.table_id)
                    dp.send_msg(msg)
        elif ev.msg.body.exp_type==1:
            stat = osparser.OFPGlobalStateStats.parser(ev.msg.body.data, offset=0)
            msg = osparser.OFPExpResetGlobalState(datapath=dp)
            dp.send_msg(msg)
            self.configure_global_states(dp)


    def timeout_probe(self,timeout):
        f_t_parser.selected_timeout = timeout

        for datapath_id in self.flow_entries_with_timeout_dict[timeout]:
            for table_id in self.flow_entries_with_timeout_dict[timeout][datapath_id]:
                for match in self.flow_entries_with_timeout_dict[timeout][datapath_id][table_id]:
                    mod = ofparser.OFPFlowMod(
                        datapath=self.dp_dictionary[datapath_id], cookie=0, cookie_mask=0, table_id=table_id,
                        command=ofproto.OFPFC_MODIFY, idle_timeout=0, hard_timeout=0,
                        priority=self.flow_entries_with_timeout_dict[timeout][datapath_id][table_id][match]['priority'], buffer_id=ofproto.OFP_NO_BUFFER,
                        out_port=ofproto.OFPP_ANY,
                        out_group=ofproto.OFPG_ANY,
                        flags=0, match=match, instructions=self.flow_entries_with_timeout_dict[timeout][datapath_id][table_id][match]['inst'])
                    self.dp_dictionary[datapath_id].send_msg(mod)

    def timeout_burst(self,burst):
        f_t_parser.selected_burst = burst

        for datapath_id in self.flow_entries_with_burst_dict[burst]:
            for table_id in self.flow_entries_with_burst_dict[burst][datapath_id]:
                for match in self.flow_entries_with_burst_dict[burst][datapath_id][table_id]:
                    mod = ofparser.OFPFlowMod(
                        datapath=self.dp_dictionary[datapath_id], cookie=0, cookie_mask=0, table_id=table_id,
                        command=ofproto.OFPFC_MODIFY, idle_timeout=0, hard_timeout=0,
                        priority=self.flow_entries_with_burst_dict[burst][datapath_id][table_id][match]['priority'], buffer_id=ofproto.OFP_NO_BUFFER,
                        out_port=ofproto.OFPP_ANY,
                        out_group=ofproto.OFPG_ANY,
                        flags=0, match=match, instructions=self.flow_entries_with_burst_dict[burst][datapath_id][table_id][match]['inst'])
                    self.dp_dictionary[datapath_id].send_msg(mod)

    def send_state_stats_request(self):
        for datapath_id in self.dp_dictionary:
            req = osparser.OFPExpStateStatsMultipartRequest(datapath=self.dp_dictionary[datapath_id])
            self.dp_dictionary[datapath_id].send_msg(req)

    def send_global_state_stats_request(self):
        for datapath_id in self.dp_dictionary:
            req = osparser.OFPExpGlobalStateStatsMultipartRequest(datapath=self.dp_dictionary[datapath_id])
            self.dp_dictionary[datapath_id].send_msg(req)

    def _monitor(self,datapath):
        hub.sleep(5)
        print("Network is ready")
        
        # This is the main traffic, used to generate HB requests/reply
        print("\nStarting traffic from h3 to h6...")
        cmd = 'sudo nice --20 nping --rate '+str(TRAFFIC_RATE)+' --count 0 --icmp-type 0 --quiet '+self.mn_net['h'+str(6)].IP()+'&'
        print('h3# '+cmd)
        self.mn_net['h3'].cmd(cmd)
        hub.sleep(1)
                
        pcap_dir = "/home/mininet/spider/results/fig8/HB_req_TO_"+delta_6+"/realiz_"+realiz_num
        if not os.path.exists(pcap_dir):
                os.makedirs(pcap_dir)
                
        print("\nStarting tshark...")
        os.system("touch "+pcap_dir+"/ping.pcap")
        os.system("sudo tshark -i s3-eth3 -n -w "+pcap_dir+"/ping.pcap 2> /dev/null &")
        hub.sleep(1)

        # This is the reverse traffic preventing the generation of HB packets
        print("\nStarting traffic from h4 to h7...")
        cmd = 'sudo nice --20 python decr_nping.py '+self.mn_net['h'+str(7)].IP()+' '+PEAK_RATE+' '+STEP+'&'
        print('h4# '+cmd)
        self.mn_net['h4'].cmd(cmd)
        
        print 'Waiting',5 + (int(PEAK_RATE)/int(STEP)) + 5,'seconds...'
        hub.sleep( 5 + (int(PEAK_RATE)/int(STEP)) + 5 )
            
        os.system("sudo kill -9 $(pidof tshark) 2> /dev/null")
        hub.sleep(1)     

        os.system("sudo kill -9 $(pidof -x ryu-manager) 2> /dev/null")
        os.system("sudo kill -9 $(pidof -x ofdatapath) 2> /dev/null")
        os.system("sudo mn -c 2> /dev/null")